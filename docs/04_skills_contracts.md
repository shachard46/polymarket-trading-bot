# OpenClaw Skill Contracts

Agents must interact with the system via these explicit OpenClaw Skills. For each skill, the system requires a `SKILL.md` defining its interface, alongside its execution logic.

## 1. Skill: evaluate_market_metrics

```yaml
---
name: evaluate_market_metrics
description: Loads market history from polymarket-scraper via poly-scan, computes quantitative signal features, and returns a compact signal_bundle for agent reasoning. Does not decide pass/fail for soft filters — only deterministic hard_veto for arbitrage and insufficient data.
---
```

**Data loading:** The skill owns DB access (`poly-scan get_market_trends`, `poly-scan get_market`). The Orchestrator passes only `market_id` and optional threshold overrides.

**Input Schema:**

```json
{
  "market_id": "string",
  "filter_overrides": { "breakout_pct_shift": 0.12 }
}
```

- `filter_overrides` is optional; merged over `config/trading_constants.FILTERS` inside the skill.
- The Orchestrator passes the parsed `## Filter Weightings` YAML block as this dict.

**Output Schema:**

```json
{
  "market_id": "string",
  "latest_snapshot": {
    "datetime": "string",
    "midpoint": 0.42,
    "volume": 125000,
    "liquidity": 3200,
    "spread": 0.02,
    "yes_price": 0.41,
    "no_price": 0.59,
    "days_since_creation": 14,
    "total_volume": 125000
  },
  "hard_veto": {
    "passed": true,
    "trigger": "arbitrage",
    "details": "yes+no=0.96 < 0.98"
  },
  "signals": {
    "volume_shock": { "ratio": 2.1, "threshold": 3.0, "baseline_median": 1200, "current": 2520 },
    "breakout": { "pct_move": 0.08, "threshold": 0.10, "window_hrs": 4, "start_price": 0.05, "end_price": 0.054 },
    "spread_anomaly": { "ratio": 1.4, "threshold": 2.0, "baseline_median": 0.02, "current": 0.028 },
    "low_liquidity_breakout": { "liquidity": 900, "pct_move": 0.07, "threshold": 0.05, "fired": false },
    "info_drift": { "max_run": 8, "threshold": 10, "direction": "up", "net_pct_change": 0.12, "proxy": "snapshot" }
  },
  "thresholds_applied": {},
  "data_quality": {
    "snapshots_used": 187,
    "oldest": "string",
    "newest": "string",
    "start_date_available": true
  },
  "error": null
}
```

**Field notes:**

- `latest_snapshot.days_since_creation` — `(now - Market.start_date)` from `poly-scan get_market`; `null` if `start_date` missing. Do **not** use API end dates.
- `latest_snapshot.total_volume` — cumulative volume from the latest trend row.
- `signals.breakout.start_price` / `end_price` — midpoint at window start and latest.
- `signals.info_drift.net_pct_change` — absolute percent midpoint change over the drift run window.
- `hard_veto.passed` — `true` for arbitrage pass, `false` for insufficient data hard reject, `null` when agent must reason over soft signals.

**Error strings (exact literals, case-sensitive):**

- `"DB_LOCKED"` — SQLite busy/locked or `poly-scan` subprocess timeout.
- `"INSUFFICIENT_DATA"` — empty or below-minimum trend series.
- On non-null `error`, `hard_veto.passed` is `null` and `signals` may be partial/empty.

**Hard veto (deterministic, agent cannot override):**

- **Pass:** `arbitrage` only when `yes_price + no_price < arbitrage_max_combined_ask`.
- **Reject:** insufficient data (`snapshots_used` below minimum).
- All other filters appear in `signals` only; the Evaluator agent decides pass/fail.

## 2. Skill: calculate_trade_allocation

```yaml
---
name: calculate_trade_allocation
description: Deterministic math engine. Use this to calculate the exact USD allocation for a trade based on the researcher's probability (p) and live market conditions.
---
```

* **Input Schema:** `{"p": float, "q": float, "D": int, "L": float, "V": float}`
* **Output Schema:** `{"allocation_usd": float, "score": float, "below_edge_threshold": bool | null, "error": str | null}` — `below_edge_threshold` is `true` when the score is at or below the minimum edge threshold `S_0` (no allocation for that reason), `false` when above threshold, and `null` when the score could not be computed (same cases as a non-null `error`).

## 3. Skill: execute_polymarket_trade

```yaml
---
name: execute_polymarket_trade
description: Polymarket API wrapper. Use this to officially place a trade on the Polymarket exchange.
---
```

* **Input Schema:** `{"market_id": str, "outcome": str, "amount": float}`
* **Output Schema:** `{"success": bool, "transaction_hash": str | null, "error": str | null}`

## 4. Skill: execute_aiq_query

```yaml
---
name: execute_aiq_query
description: Deep qualitative research engine. Use this to perform exhaustive, unconstrained research on fundamental market conditions and generate thesis points using nvidia a-iq framework.
---
```

* **Input Schema:** `{"query": str}`
* **Output Schema:** `{"research_data": str, "error": str | null}`
* **Runtime config:** base URL and polling defaults read from `config/trading_constants` (`AIQ_BASE_URL`, `AIQ_POLL_INTERVAL_SEC`, `AIQ_TIMEOUT_SEC`); each overridable via the matching environment variable.

**Invocation:**

- **Command:** `python3 {baseDir}/run.py '<json>'`
- **Args JSON:** `{"query": "<focused research question>"}`
- **Return:** parse stdout as JSON `{"research_data": str, "error": str | null}`
