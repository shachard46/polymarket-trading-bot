# Tools

Declared tool allowlist lives in `agent.yaml` (`tools.allow`). It is documentation / agent-runtime configuration only; the orchestrator does not enforce it at runtime.

- `evaluate_market_metrics`: pass `{ market_id, filter_overrides: filter_directives }`.
