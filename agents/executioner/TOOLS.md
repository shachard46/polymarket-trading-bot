# Tools

Source of truth: runtime-enforced tool access is defined in `agent.yaml` and validated by the orchestrator.

- `calculate_trade_allocation` then `execute_polymarket_trade` only when allocation_usd > 0.
