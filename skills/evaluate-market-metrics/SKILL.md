---
name: evaluate_market_metrics
description: Loads market history from polymarket-scraper via poly-scan, computes quantitative signal features, and returns a compact signal_bundle for agent reasoning. Hard veto only for arbitrage and insufficient data.
---

- **Input Schema:** `{"market_id": str, "filter_overrides": dict | null}`
  - `filter_overrides` optional; merged over `config/trading_constants.FILTERS`.
- **Output Schema:** signal_bundle with `market_id`, `latest_snapshot`, `hard_veto`, `signals`, `thresholds_applied`, `data_quality`, `error`.
- **Errors:** `"DB_LOCKED"` | `"INSUFFICIENT_DATA"` | null (exact literals).
- **Hard veto:** arbitrage pass (`hard_veto.passed=true`) or insufficient data reject (`hard_veto.passed=false`). Agent judges all soft signals.
- **Signals:** `volume_shock`, `breakout` (with `start_price`/`end_price`), `spread_anomaly`, `low_liquidity_breakout`, `info_drift` (with `net_pct_change`).
- **Context:** `latest_snapshot.days_since_creation` from `start_date`; `total_volume` from latest trend row.

## Invocation

- **Command:** `python3 {baseDir}/run.py '<json>'`
- **Args JSON:** `{"market_id": "<from input>", "filter_overrides": <filter_directives or null>}`
- **Env:** `PYTHONPATH` must include the directory containing `config/trading_constants.py`; set `POLY_SCAN_BIN` or ensure `poly-scan` is on `PATH`
- **Return:** parse stdout as the signal_bundle; do not read source files or recompute metrics
