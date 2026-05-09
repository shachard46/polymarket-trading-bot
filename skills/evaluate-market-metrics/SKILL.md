---
name: evaluate_market_metrics
description: Evaluates a Polymarket market against 6 core quantitative strategies. Use this to determine if a market passes the filter threshold. Receives a chronological series of market snapshots sourced by the Orchestrator from the polymarket-scraper (poly-scan get_market_trends, reversed oldest-first).
---

- **Input Schema:** `{"historic_market_data": list[dict]}`
  - Each dict: `{"datetime": str, "yes_price": float, "no_price": float, "volume": float, "liquidity": float, "last_trade_price": float, "midpoint": float, "spread": float}`
  - Snapshots must be oldest-first. The Orchestrator reverses the scraper's newest-first output before calling this skill.
- **Output Schema:** `{"passed": bool, "trigger": str | null, "confidence_multiplier": float, "details": str, "error": str | null}`
- **Filters (first fire wins):** `arbitrage`, `volume_shock`, `breakout`, `spread_anomaly`, `low_liquidity_breakout`, `info_drift`
- **Thresholds:** all read from `config/trading_constants.FILTERS`
