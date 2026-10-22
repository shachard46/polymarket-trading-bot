# Polymarket Hub-and-Spoke Trading Architecture

## 1. Core Infrastructure

- **Framework:** Python backend utilizing the local OpenClaw Gateway via the `openclaw` CLI.
- **Design Pattern:** Hub-and-Spoke. The central Orchestrator (`main.py`) controls execution flow. Agents are strictly ephemeral functions.
- **Lifecycle:** Orchestrator creates JSON input -> invokes `openclaw agent --agent <id>` -> Agent executes Tools -> Agent returns JSON or Markdown -> Orchestrator parses output.
- **Tooling:** Agents do not perform math, execute API calls natively, or manage state. They must use the deterministic Python tools defined in `04_skills_contracts.md`.
- **Quantitative inputs:** Phase 2 is deterministic Hub code (`orchestrator/gate.py`): eligibility, a ranking score, per-market backoff, and an arbitrage check, followed by the price-blind `triage` agent. The `evaluate_market_metrics` skill is a feature source only: the Hub reads its signal bundle (loaded from the polymarket-scraper SQLite DB via `poly-scan`) for a small ranking bonus. No agent consumes it, and agents never query the scraper directly.

## 2. File System Memory & The Pydantic Gatekeeper

All system states, research, and logs are maintained in a local Obsidian Vault.

- **CRITICAL RULE:** Agents NEVER write directly to the file system. Agents return raw dictionary payloads to the Orchestrator.
- **The Gatekeeper:** The `ObsidianManager` utility uses strict `Pydantic` models to validate the agent's payload. If the payload fails schema validation, the write is aborted, and the market is flagged `status: inactive` in its native vault directory.

## 3. Obsidian Vault Directory Schemas

- **`/Vault/00_System/active_directives.md`**: Overwritten exclusively by the Overseer. Format: YAML frontmatter with strict config thresholds.
- **`/Vault/01_Filters/`**: Logs detailing passed quantitative evaluations. Format: Pure JSON (or `.md` with only YAML frontmatter).
- **`/Vault/02_Active_Research/`**: Active workspace. Format: Strict YAML Frontmatter containing `market_id` and `estimated_p`, followed by Markdown headers (`## Bull Thesis`, `## Bear Thesis`, `## Post-Mortem`).
- **`/Vault/03_Trades/`**: Execution logs and position-sizing calculations. Format: Pure JSON deterministic dump.
- **`/Vault/04_Post_Mortems/`**: Resolved markets moved here. The Post-Mortem analyst appends text directly under the existing `## Post-Mortem` header.

## 4. The Cold Start Protocol

Upon initialization, if `/00_System/active_directives.md` does not exist or is empty, the `ObsidianManager` MUST generate a default seed file containing baseline instructions for the Deep Researcher (e.g., standard fundamental analysis protocols and neutral filter weightings). This provides the initial state until the Overseer completes its first learning loop.
