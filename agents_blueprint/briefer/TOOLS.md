# Tools

Source of truth: runtime-enforced tool access is defined in `agent.yaml` and validated by the orchestrator.

No runtime tools. The Hub runs `execute_aiq_query` in parallel from your `research_queries`; you only emit the planning JSON.
