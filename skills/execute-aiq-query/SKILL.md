---
name: execute_aiq_query
description: Deep qualitative research engine. Use for exhaustive fundamental research on market conditions (NVIDIA A-IQ). Combine findings before writing bull/bear theses.
---

- **Input Schema:** `{"query": str}` — focused research question(s); prefer 2–3 strong queries over many shallow ones.
- **Output Schema:** `{"research_data": str, "error": str | null}`
- **Usage:** Deep Researcher may call at most **4** times per orchestrator invocation.
- **Runtime:** Requires local A-IQ (`nat serve`). Env: `AIQ_BASE_URL`, `AIQ_POLL_INTERVAL_SEC`, `AIQ_TIMEOUT_SEC` (defaults in `config/trading_constants`).
- **On failure:** If `error` is set or `research_data` is empty, follow AGENTS.md calibration (defer to market-implied price; do not invent 0.5).

## Invocation

- **Command:** `python3 {baseDir}/run.py '<json>'`
- **Args JSON:** `{"query": "<focused research question>"}`
- **Return:** parse stdout as JSON `{"research_data": str, "error": str | null}`; do not re-run A-IQ or read source files
