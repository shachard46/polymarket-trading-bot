# Tools

Declared tool allowlist lives in `agent.yaml` (`tools.allow`). It is documentation / agent-runtime configuration only; the orchestrator does not enforce it at runtime.

No runtime tools. The Orchestrator fetches A-IQ data into `research_bundle` (and runs new queries from `new_queries`); you synthesize from that bundle only.
