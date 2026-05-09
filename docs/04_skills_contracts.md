# OpenClaw Skill Contracts

Agents must interact with the system via these explicit OpenClaw Skills. For each skill, the system requires a `skill.md` defining its interface, alongside its execution logic.

## 1. Skill: evaluate_market_metrics
```yaml
---
name: evaluate_market_metrics
description: Evaluates a Polymarket market against 6 core quantitative strategies. Use this to determine if a market passes the filter threshold. Receives a chronological series of market snapshots sourced by the Orchestrator from the polymarket-scraper.
---

```

* **Input Schema:** `{"historic_market_data": list[dict]}`
  * Each dict: `{"datetime": str, "yes_price": float, "no_price": float, "volume": float, "liquidity": float, "last_trade_price": float, "midpoint": float, "spread": float}`
  * Snapshots must be **oldest-first**. The Orchestrator reverses the scraper's newest-first output before passing.
* **Output Schema:** `{"passed": bool, "trigger": str | null, "confidence_multiplier": float, "details": str, "error": str | null}`

## 2. Skill: search_market_context

```yaml
---
name: search_market_context
description: Web scraping skill. Use this to search the internet for real-time news, context, and current events regarding a specific Polymarket title.
---

```

* **Input Schema:** `{"query": str}`
* **Output Schema:** `{"summary": str, "error": str | null}`

## 3. Skill: calculate_trade_allocation

```yaml
---
name: calculate_trade_allocation
description: Deterministic math engine. Use this to calculate the exact USD allocation for a trade based on the researcher's probability (p) and live market conditions.
---

```

* **Input Schema:** `{"p": float, "q": float, "D": int, "L": float, "V": float}`
* **Output Schema:** `{"allocation_usd": float, "score": float, "error": str | null}`

## 4. Skill: execute_polymarket_trade

```yaml
---
name: execute_polymarket_trade
description: Polymarket API wrapper. Use this to officially place a trade on the Polymarket exchange.
---

```

* **Input Schema:** `{"market_id": str, "outcome": str, "amount": float}`
* **Output Schema:** `{"success": bool, "transaction_hash": str | null, "error": str | null}`

## 5. Skill: execute_aiq_query

```yaml
---
name: execute_aiq_query
description: Deep qualitative research engine. Use this to perform exhaustive, unconstrained research on fundamental market conditions and generate thesis points using nvidia a-iq framework.
---

```

* **Input Schema:** `{"query": str}`
* **Output Schema:** `{"research_data": str, "error": str | null}`
* **Runtime config:** base URL and polling defaults read from `config/trading_constants` (`AIQ_BASE_URL`, `AIQ_POLL_INTERVAL_SEC`, `AIQ_TIMEOUT_SEC`); each overridable via the matching environment variable.
