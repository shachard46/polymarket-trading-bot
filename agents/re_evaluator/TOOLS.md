# Tools

Source of truth: runtime-enforced tool access is defined in `agent.yaml` and validated by the orchestrator.

- `evaluate_market_metrics`: same as evaluator; used for every Re-Evaluator run (`review_kind: quantitative` or `edge_research_refresh`). In `edge_research_refresh`, the orchestrator also passes prior filter log, active research markdown, and trade log for regime-staleness context; the pass/fail fields still mirror the tool output.
