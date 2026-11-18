# Polymarket Hub-and-Spoke Trading Architecture

## 1. Core Infrastructure

- **Framework:** Python backend utilizing the local OpenClaw Gateway via the `openclaw` CLI.
- **Design Pattern:** Hub-and-Spoke. The central Orchestrator (`main.py`) controls execution flow. Agents are strictly ephemeral functions.
- **Lifecycle:** Orchestrator creates JSON input -> invokes `openclaw agent --agent <id>` -> Agent executes Tools -> Agent returns JSON -> Orchestrator parses output.
- **Tooling:** Agents do not perform math, execute API calls natively, or manage state. Math and trading are deterministic Hub code over the skills in `04_skills_contracts.md` (Phase 4 has no agent).
- **Price-blind models:** no agent payload contains the market price `q`. Models estimate from market text and evidence; the Hub compares the estimate to `q` (divergence, misread and sample-spread guards) and applies the lambda-shrink toward `q` when sizing. See `02_orchestrator_pipeline.md` section 3.
- **Quantitative inputs:** Phase 2 is deterministic Hub code (`orchestrator/gate.py`): eligibility, a ranking score, per-market backoff, and an arbitrage check, followed by the price-blind `triage` agent. Market data comes straight from Polymarket's public APIs (`orchestrator/polymarket_api.py`: Gamma for candidates/prices/resolution, CLOB for executable asks); there is no scraper DB or momentum signal any more. Agents never query Polymarket directly.

## 2. File System Memory & The Pydantic Gatekeeper

All system states, research, and logs are maintained in a local Obsidian Vault.

- **CRITICAL RULE:** Agents NEVER write directly to the file system. Agents return raw dictionary payloads to the Orchestrator.
- **The Gatekeeper:** The `ObsidianManager` utility uses strict `Pydantic` models to validate the agent's payload. If the payload fails schema validation, the write is aborted, and the market is flagged `status: inactive` in its native vault directory.

## 3. Obsidian Vault Directory Schemas

- **`/Vault/00_System/active_directives.md`**: Overwritten exclusively by the Overseer. Format: YAML frontmatter with strict config thresholds.
- **`/Vault/01_Filters/`**: Logs detailing passed quantitative evaluations. Format: Pure JSON (or `.md` with only YAML frontmatter).
- **`/Vault/02_Active_Research/`**: Active workspace. Format: Strict YAML Frontmatter containing `market_id`, `estimated_p`, `p_low`, `p_high`, `confidence`, `as_of`, and the Hub-side `q_at_research` / `researched_at`, followed by Markdown headers (`## Bull Thesis`, `## Bear Thesis`, `## Post-Mortem`). Also holds `aiq_cache.json` (24h A-IQ query cache) and `research_bundles/`.
- **`/Vault/03_Trades/`**: Execution logs and position-sizing calculations (`side`, `price`, `shares`, `p`, `p_low`, `p_high`, `q_entry`, `executed`, `transaction_hash`). Format: Pure JSON deterministic dump written by the Hub.
- **`/Vault/04_Post_Mortems/`**: Resolved markets moved here. The Post-Mortem analyst appends text directly under the existing `## Post-Mortem` header.

## 4. The Cold Start Protocol

Upon initialization, if `/00_System/active_directives.md` does not exist or is empty, the `ObsidianManager` MUST generate a default seed file containing baseline instructions for the Deep Researcher (e.g., standard fundamental analysis protocols and neutral filter weightings). This provides the initial state until the Overseer completes its first learning loop.
