# Tools

Declared tool allowlist lives in `agent.yaml` (`tools.allow`). It is documentation / agent-runtime configuration only; the orchestrator does not enforce it at runtime.

- `execute_polymarket_trade` only when the Hub-provided `allocation.allocation_usd` > 0 and `paper_trade_mode` is false. The Hub computes allocation (`calculate_trade_allocation`) itself.
