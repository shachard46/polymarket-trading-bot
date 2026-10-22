---
name: calculate_trade_allocation
description: Deterministic math engine. Use this to calculate the exact USD allocation for a trade based on the researcher's probability (p) and live market conditions.
---

* **Input Schema:** `{"p": float, "q": float, "D": int, "L": float, "V": float, "remaining_bankroll": float | null}` — `remaining_bankroll` optional (default `BANKROLL_USD`). Validated: `0<p<=1`, `0<q<1`, `D>=0`, `L>0`, `V>0`, all finite; invalid input returns `error` with zero allocation.
* **Output Schema:** `{"allocation_usd": float, "score": float, "below_edge_threshold": bool | null, "error": str | null}`
* **Cap:** `allocation_usd = min(f*BANKROLL_USD, F_MAX*BANKROLL_USD, LIQ_FRAC*L, remaining_bankroll)`; zero when `S` is non-finite or `S <= S_0`.

## Invocation

- **Command:** `python3 {baseDir}/run.py '<json>'`
- **Args JSON:** `{"p": <float>, "q": <float>, "D": <int>, "L": <float>, "V": <float>}` (optionally `"remaining_bankroll"`)
- **Return:** parse stdout as JSON `{"allocation_usd": float, "score": float, "below_edge_threshold": bool | null, "error": str | null}`; do not read source files or recompute the math
