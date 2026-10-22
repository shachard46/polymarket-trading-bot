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
    "volume_shock": { "ratio": 2.1, "fired": false, "threshold": 2.5, "baseline_median": 1200, "current": 2520 },
    "breakout": { "pct_move": 0.08, "threshold": 0.08, "window_hrs": 4, "start_price": 0.05, "end_price": 0.054 },
    "spread_anomaly": { "ratio": 1.4, "threshold": 2.0, "baseline_median": 0.02, "current": 0.028 },
    "low_liquidity_breakout": { "liquidity": 900, "pct_move": 0.07, "threshold": 0.05, "fired": false },
    "info_drift": { "max_run": 8, "threshold": 9, "direction": "up", "net_pct_change": 0.12, "proxy": "snapshot" }
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
- `signals.info_drift.net_pct_change` — absolute percent price change from the start to the **end of the best run** (not the latest snapshot).
- `signals.volume_shock` — when the baseline median is `0` (or absent) no division is done: `ratio` is `null` and `fired` is `true` iff current volume `> 0`.
- `low_liquidity_breakout` treats a missing latest price as missing (not `0`); a reference snapshot more than 50% off the requested age is rejected (treated as no reference).
- `filter_overrides` values are coerced to the type of the default in `FILTERS`; unknown keys, non-numeric or negative values are ignored (logged) and the default is used.
- `hard_veto.passed` — `true` for arbitrage pass, `false` for insufficient data hard reject, `null` when agent must reason over soft signals.

**Error strings (exact literals, case-sensitive):**

- `"DB_LOCKED"` — SQLite busy/locked or `poly-scan` subprocess timeout.
- `"SCANNER_ERROR"` — `poly-scan` binary missing, nonzero exit (not a lock), invalid JSON, market not found, or an unexpected exception inside the skill.
- `"INSUFFICIENT_DATA"` — empty or below-minimum trend series.
- On non-null `error`, `hard_veto.passed` is `null` and `signals` may be partial/empty.

**Hard veto (deterministic, agent cannot override):**

- **Pass:** `arbitrage` only when both `yes_price` and `no_price` are numbers in `(0, 1)` and `yes_price + no_price < arbitrage_max_combined_ask`.
- **Reject:** insufficient data (`snapshots_used` below minimum).
- All other filters appear in `signals` only; the Evaluator agent decides pass/fail.

## 2. Skill: calculate_trade_allocation

```yaml
---
name: calculate_trade_allocation
description: Deterministic math engine. Use this to calculate the exact USD allocation for a trade based on the researcher's probability (p) and live market conditions.
---
```

* **Input Schema:** `{"p": float, "p_low": float|null, "p_high": float|null, "q_yes_ask": float, "q_no_ask": float|null, "L": float, "D": number, "bankroll": float, "open_exposure_total": float, "open_exposure_event": float, "lam": float|null}` — validated: `0 <= p <= 1`, `0 < q < 1`, `L > 0`, `D >= 0`, `bankroll > 0`, exposures `>= 0`, all finite; `p_low`/`p_high` given together with `p_low <= p <= p_high`; `q_no_ask` defaults to `1 - q_yes_ask`; `lam` overrides `LAMBDA` (0.3). Legacy `q` is an alias of `q_yes_ask`. Invalid input returns zero allocation and a non-null `error`.
* **Math:** shrunk estimate, best-edge side (`edge >= MIN_EDGE = 0.04`), tax-aware (`TAX = 0.25`) quarter-Kelly; see `docs/03_strategy_math.md`.
* **Cap:** `allocation_usd = max(0, min(f*bankroll, F_MAX*bankroll, LIQ_FRAC*L, EVENT_CAP*bankroll - open_exposure_event, TOTAL_EXPOSURE_CAP*bankroll - open_exposure_total))` (0.10 / 0.05 / 0.15 / 0.60); under `MIN_ORDER_USD` -> 0.
* **Output Schema:** `{"allocation_usd", "side": "YES"|"NO"|null, "price", "shares", "p_adj", "edge", "kelly_fraction", "below_edge_threshold": bool|null, "binding_cap": str|null, "edge_per_day", "error"}` — `below_edge_threshold` is `true` when the best edge is under `MIN_EDGE`, `false` otherwise, `null` when `error` is set.

## 3. Skill: execute_polymarket_trade

```yaml
---
name: execute_polymarket_trade
description: Polymarket API wrapper. Use this to officially place a trade on the Polymarket exchange.
---
```

* **Input Schema:** `{"market_id": str (non-empty), "outcome": "YES" | "NO", "amount": float}` — `0 < amount <= MAX_ORDER_USD` (`F_MAX * BANKROLL_USD`), finite.
* **Output Schema:** `{"success": bool, "transaction_hash": str | null, "paper": bool, "error": str | null}` — `paper` is `true` when simulated.

**Invocation:** `python3 {baseDir}/run.py '<json>'` (JSON also accepted on stdin); `calculate_trade_allocation` has an equivalent `run.py`.

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
