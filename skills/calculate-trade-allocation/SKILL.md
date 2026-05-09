---
name: calculate_trade_allocation
description: Deterministic math engine. Use this to calculate the exact USD allocation for a trade based on the researcher's probability (p) and live market conditions.
---

* **Input Schema:** `{"p": float, "q": float, "D": int, "L": float, "V": float}`
* **Output Schema:** `{"allocation_usd": float, "score": float, "error": str | null}`
