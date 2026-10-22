# Tools

Declared allowlist lives in `agent.yaml` (`tools.allow`): `web_search`, `web_fetch`. Documentation / agent-runtime configuration only; the orchestrator does not enforce it.

Use `web_search` to run each query and `web_fetch` to read a promising page. Never write files. The Hub sanitizes and persists your output.
