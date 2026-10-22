---
name: execute_polymarket_trade
description: Polymarket API wrapper. Use this to officially place a trade on the Polymarket exchange.
---

* **Input Schema:** `{"market_id": str, "outcome": "YES" | "NO", "amount": float}` — `market_id` non-empty; `0 < amount <= MAX_ORDER_USD` (`F_MAX*BANKROLL_USD`), finite.
* **Output Schema:** `{"success": bool, "transaction_hash": str | null, "paper": bool, "error": str | null}`

## Invocation

- **Command:** `python3 {baseDir}/run.py '<json>'`
- **Args JSON:** `{"market_id": "<from input>", "outcome": "YES", "amount": <allocation_usd>}`
- **Return:** parse stdout as JSON `{"success": bool, "transaction_hash": str | null, "paper": bool, "error": str | null}`; `paper: true` means simulated, not a live order
