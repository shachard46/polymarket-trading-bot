---
name: execute_polymarket_trade
description: Polymarket API wrapper. Use this to officially place a trade on the Polymarket exchange.
---

* **Input Schema:** `{"market_id": str, "outcome": str, "amount": float}`
* **Output Schema:** `{"success": bool, "transaction_hash": str | null, "error": str | null}`
