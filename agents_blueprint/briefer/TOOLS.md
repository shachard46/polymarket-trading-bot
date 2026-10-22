# Tools

Declared tool allowlist lives in `agent.yaml` (`tools.allow`). It is documentation / agent-runtime configuration only; the orchestrator does not enforce it at runtime.

No runtime tools. The Hub runs `execute_aiq_query` in parallel from your `research_queries`; you only emit the planning JSON.
