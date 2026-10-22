# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -r requirements.txt            # orchestrator deps (pydantic, PyYAML); skill deps live beside each skill
python main.py                             # run the orchestrator loop (stub runner by default)
OPENCLAW_ORCHESTRATOR_MODE=live python main.py   # call real agents via the `openclaw agent` CLI

python -m pytest                           # full suite (fast, no network/DB needed)
python -m pytest tests/orchestrator/test_parse.py::test_name   # single test

python -m orchestrator.replay --market-id 0xabc [--dry-run]   # clear `status: inactive` for one market
python -m orchestrator.replay --all [--dir filters]           # clear across vault dirs

python3 skills/<skill>/run.py '<json>'     # invoke a skill directly (same entrypoint agents use)
OPENCLAW_AGENTS_ROOT=~/.openclaw/agents ./scripts/sync_openclaw_agent_workspace.sh <role|all>  # push agents_blueprint/ to OpenClaw
```

`skills/market-scraper` is a git submodule (`poly-scan` CLI + SQLite DB). It may be empty locally; `git submodule update --init` to fetch it.

## Architecture

Hub-and-Spoke LLM trading pipeline for Polymarket. Start with `docs/01_architecture.md` and `docs/02_orchestrator_pipeline.md`; `docs/03_strategy_math.md` (sizing math), `docs/04_skills_contracts.md` (skill I/O), and `docs/05_agent_schemas.md` (agent I/O) are the specs the code implements. Keep them in sync when behavior changes.

**Hub (`orchestrator/`)** — the only component that spawns agents or touches disk. `loop.run_forever` runs `run_pipeline_tick` every `PIPELINE_INTERVAL_SEC` and the Overseer every `OVERSEER_INTERVAL_SEC`. `phases.py` holds the six phases:
1. Ingest market ids from the scraper.
2. Quantitative routing → Evaluator or Re-Evaluator agent; every result is written to `01_Filters/`.
3. Qualitative "Forensic Pipeline": `briefer` (query planner) → Hub fetches A-IQ in parallel → iterative `deep_researcher` (max 4 rounds, then a forced-synthesis override) → `02_Active_Research/`.
4. Execution: Hub computes allocation (`skills/calculate-trade-allocation`) → Executioner → `03_Trades/` (idempotent via intent files).
5. Resolution → move to `04_Post_Mortems/` and run the Post-Mortem Analyst.
6. Overseer rewrites `00_System/active_directives.md` from the post-mortems.

**Spokes (`agents_blueprint/<role>/`)** — each role has `AGENTS.md` (prompt), `TOOLS.md`, and `agent.yaml` (`openclaw_agent_id`, tool allowlist, `input_schema` and `output_schema`). `agents_blueprint/loader.py` builds `AGENTS` and turns the YAML schemas into Pydantic models. `runner.spawn_agent` validates the input against those models before invoking the agent. Agents never write files and never do math. They call deterministic skills in `skills/` and return JSON or Markdown, which the Hub parses (`parse.py`, `agent_outputs.py`) and validates (`schema_validation.py`).

**Runner modes** (`OPENCLAW_ORCHESTRATOR_MODE`): `stub` (default, canned schema-valid responses), `stub_error` (forces the error path), and `live`. Unknown values raise an error and never fall back to stub silently.

**Vault I/O (`obsidian_utils.ObsidianManager`)** — the single gatekeeper for all writes. It uses Pydantic payload models and atomic writes. The vault root is `OPENCLAW_VAULT_PATH`, or the repo root by default, with subdirectories `Vault/00_System` … `Vault/04_Post_Mortems` (`config/trading_constants.VAULT_PATHS`). Tests should point it at a tmp dir.

**Error model: an in-place state machine with no error directory.** When an agent returns an error or parsing or validation fails, the Hub sets `status: inactive` and `error_log` on that market's own artifact (YAML frontmatter, or the JSON root for trades) and moves on to the next market. Inactive markets are skipped until `orchestrator.replay` clears the flags. Recent commits draw a deliberate line here: infra/transport failures are retried, content failures mark the market inactive, and soft failures are routed to the Re-Evaluator. The Executioner is deliberately *not* retried. Keep these categories separate.

**Config:** trading and strategy constants (`F_MAX`, `LIQ_FRAC`, `FILTERS`, `PAPER_TRADE_MODE`, A-IQ settings) are in `config/trading_constants.py`. Env-driven tunables (`OPENCLAW_*`: timeouts, retries, `OPENCLAW_TOP_MARKETS`, `OPENCLAW_AUTO_REPLAY`, edge-refresh cap) are in `orchestrator/config.py`.

## Testing notes

- `tests/orchestrator/conftest.py` autouse-stubs the evaluator signal-bundle fetch (`set_fetch_signal_bundle_impl`) so phase tests never touch the scraper DB.
- Skill tests use fixtures under `tests/skills/fixtures/`.

## Knowledge graph (from `.cursor/rules/graphify.mdc`)

`graphify-out/` holds a code knowledge graph. For architecture questions, try `graphify query "<question>"` / `graphify path "<A>" "<B>"` / `graphify explain "<concept>"` before broad grepping, and read `graphify-out/GRAPH_REPORT.md` only for wide reviews. After changing code, run `graphify update .` (AST-only, no API cost).
