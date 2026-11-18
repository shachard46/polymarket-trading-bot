---
name: calculate_trade_allocation
description: Deterministic math engine. Use this to size a trade (side, USD, shares) from the researcher's blind probability estimate and live market prices. Tax-aware fractional Kelly with shrinkage and exposure caps.
---

* **Input Schema:** `{"p": float, "p_low": float|null, "p_high": float|null, "q_yes_ask": float, "q_no_ask": float|null, "L": float, "D": number, "bankroll": float, "open_exposure_total": float, "open_exposure_event": float, "lam": float|null, "min_edge": float|null}` — `0<=p<=1`, `0<q<1`, `L>0`, `D>=0`, `bankroll>0`, exposures `>=0`, all finite; `p_low/p_high` together with `p_low<=p<=p_high`; `q_no_ask` defaults to `1-q_yes_ask`; `lam` / `min_edge` default to `effective_lambda()` / `effective_min_edge()` (paper: `PAPER_LAMBDA` 0.5 / `PAPER_MIN_EDGE` 0.03; live: `LAMBDA` 0.3 / `MIN_EDGE` 0.04) and the output echoes the values used. The legacy keyword `q` is accepted as an alias of `q_yes_ask`. Invalid input returns `error` with zero allocation.
* **Output Schema:** `{"allocation_usd", "side": "YES"|"NO"|null, "price", "shares", "p_adj", "edge", "kelly_fraction", "below_edge_threshold": bool|null, "binding_cap": str|null, "edge_per_day", "error"}`
* **Math:** see `docs/03_strategy_math.md`. `p_adj = q + LAMBDA*(p-q)*max(0,1-w/0.5)`; best-edge side; edge >= `MIN_EDGE`; pre-tax Kelly x `KELLY_FRAC`; capped by `F_MAX`, `LIQ_FRAC*L`, `EVENT_CAP`, `TOTAL_EXPOSURE_CAP`; below `MIN_ORDER_USD` -> 0. `D` only feeds the informational `edge_per_day`.

## Invocation

- **Command:** `python3 {baseDir}/run.py '<json>'`
- **Return:** parse stdout as the output JSON above; do not read source files or recompute the math
