# Tools

Source of truth: runtime-enforced tool access is defined in `agent.yaml` and validated by the orchestrator.

- `evaluate_market_metrics`: pass `{ market_id, filter_overrides: filter_directives }`. Compare current signal_bundle vs `historic_signal_bundle` from the vault for regime-change decisions.
