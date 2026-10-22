# OpenClaw Skill Contracts

Agents must interact with the system via these explicit OpenClaw Skills. For each skill, the system requires a `SKILL.md` defining its interface, alongside its execution logic.

## 1. Skill: calculate_trade_allocation

```yaml
---
name: calculate_trade_allocation
description: Deterministic math engine. Use this to calculate the exact USD allocation for a trade based on the researcher's probability (p) and live market conditions.
---
```

* **Input Schema:** `{"p": float, "p_low": float|null, "p_high": float|null, "q_yes_ask": float, "q_no_ask": float|null, "L": float, "D": number, "bankroll": float, "open_exposure_total": float, "open_exposure_event": float, "lam": float|null, "tax_mode": "annual_offset"|"per_trade"|null}` — validated: `0 <= p <= 1`, `0 < q < 1`, `L > 0`, `D >= 0`, `bankroll > 0`, exposures `>= 0`, all finite; `p_low`/`p_high` given together with `p_low <= p <= p_high`; `q_no_ask` defaults to `1 - q_yes_ask`; `lam` overrides `LAMBDA` (0.3); `tax_mode` overrides `TAX_MODE` (default `annual_offset`: pre-tax Kelly odds). Legacy `q` is an alias of `q_yes_ask`. Invalid input returns zero allocation and a non-null `error`.
* **Math:** shrunk estimate, best-edge side (`edge >= MIN_EDGE = 0.04`), quarter-Kelly (pre-tax odds in `annual_offset` mode, per-trade tax-adjusted in `per_trade`); see `docs/03_strategy_math.md`.
* **Cap:** (Phase 4 passes `L = min(liquidity, depth_usd_within_2c / LIQ_FRAC)` when the ask depth within 2c of the chosen side is smaller than the order, so `LIQ_FRAC*L` doubles as the depth cap.) `allocation_usd = max(0, min(f*bankroll, F_MAX*bankroll, LIQ_FRAC*L, EVENT_CAP*bankroll - open_exposure_event, TOTAL_EXPOSURE_CAP*bankroll - open_exposure_total))` (0.10 / 0.05 / 0.15 / 0.60); under `MIN_ORDER_USD` -> 0.
* **Output Schema:** `{"allocation_usd", "side": "YES"|"NO"|null, "price", "shares", "p_adj", "edge", "kelly_fraction", "below_edge_threshold": bool|null, "binding_cap": str|null, "edge_per_day", "error"}` — `below_edge_threshold` is `true` when the best edge is under `MIN_EDGE`, `false` otherwise, `null` when `error` is set.

## 2. Skill: execute_polymarket_trade

```yaml
---
name: execute_polymarket_trade
description: Polymarket API wrapper. Use this to officially place a trade on the Polymarket exchange.
---
```

* **Input Schema:** `{"market_id": str (non-empty), "outcome": "YES" | "NO", "amount": float, "price": float | null}` — `0 < amount <= MAX_ORDER_USD` (`F_MAX * BANKROLL_USD`), finite; `price` (optional, `0 < price < 1`) is the chosen side's ask, informational for paper trades.
* **Output Schema:** `{"success": bool, "transaction_hash": str | null, "paper": bool, "error": str | null}` — `paper` is `true` when simulated.

**Callers:** the Hub only. Phase 4 loads the module in-process (like `calculate_trade_allocation`) and calls it with the `compute_allocation` result (`outcome` = side, `amount` = `allocation_usd`, `price`); the trade log records `executed = success and not paper`. No agent has this tool any more.

**Invocation (manual):** `python3 {baseDir}/run.py '<json>'` (JSON also accepted on stdin); `calculate_trade_allocation` has an equivalent `run.py`.

## 3. Skill: execute_aiq_query

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
