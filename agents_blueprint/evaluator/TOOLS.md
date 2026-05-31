# Tools

Source of truth: runtime-enforced tool access is defined in `agent.yaml` and validated by the orchestrator.

- `evaluate_market_metrics`: pass `{ market_id, filter_overrides: filter_directives }`. Skill loads DB history internally and returns signal_bundle.
