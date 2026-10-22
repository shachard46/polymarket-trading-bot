# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -r requirements.txt            # orchestrator deps (pydantic, PyYAML, requests); skill deps live beside each skill
python main.py                             # run the orchestrator loop (stub runner by default)
OPENCLAW_ORCHESTRATOR_MODE=live python main.py   # call real agents via the `openclaw agent` CLI

python -m pytest tests                     # full suite (fast, no network needed; bare `pytest` also collects the scraper submodule and breaks)
python -m pytest tests/orchestrator/test_parse.py::test_name   # single test

python -m orchestrator.replay --market-id 0xabc [--dry-run]   # clear `status: inactive` for one market
python -m orchestrator.replay --all [--dir filters]           # clear across vault dirs
python -m orchestrator.replay --directives 3                  # restore archived directives v3 as a NEW version

python3 skills/<skill>/run.py '<json>'     # invoke a skill directly (same entrypoint agents use)
OPENCLAW_AGENTS_ROOT=~/.openclaw/agents ./scripts/sync_openclaw_agent_workspace.sh <role|all>  # push agents_blueprint/ to OpenClaw
```

`skills/market-scraper` is a git submodule (`poly-scan` CLI + SQLite DB). It is **unused** now (market data comes straight from Polymarket's public APIs via `orchestrator/polymarket_api.py`) and is kept temporarily; `/Users/shachardavid/projects/polymarket.db` is not used either.

## Architecture

Hub-and-Spoke LLM trading pipeline for Polymarket. Start with `docs/01_architecture.md` and `docs/02_orchestrator_pipeline.md`; `docs/03_strategy_math.md` (sizing math), `docs/04_skills_contracts.md` (skill I/O), and `docs/05_agent_schemas.md` (agent I/O) are the specs the code implements. Keep them in sync when behavior changes.

**Hub (`orchestrator/`)** — the only component that spawns agents or touches disk. `loop.run_forever` runs `run_pipeline_tick` every `PIPELINE_INTERVAL_SEC` and the Overseer every `OVERSEER_INTERVAL_SEC`. `orchestrator/phases/` (package; one module per phase or stage: `p1_ingest`, `p2_routing`, `p3_quick`/`p3_full`/`p3_evidence`/`p3_queue`, `p4_execution`, `p5_resolution`, `p6_overseer`, plus `common`, `agent_call`, `research_state`; `__init__` re-exports the public names) holds the six phases. Tests that monkeypatch must patch the defining submodule, not the package:
1. Ingest candidates from Polymarket's Gamma API (`orchestrator/polymarket_api.py`, wrapped by `orchestrator/scraper.py` which keeps the phase-facing signatures): open binary Yes/No markets inside the days window with enough liquidity; `market_id` is the condition id (`0x...`).
2. Gate + triage: deterministic code (`orchestrator/gate.py`: eligibility, score, backoff, arbitrage) ranks markets; the top `OPENCLAW_GATE_BUDGET` go to the price-blind `triage` agent (cached per market/event forever); the top `OPENCLAW_TRIAGE_BUDGET` researchable ones get `passed: true` in `01_Filters/` for Phase 3. Triage rejects and 6 consecutive gate fails set `status: retired`.
3. Price-blind research (models never see the market price `q`): a QUICK stage (one `deep_researcher` call, no A-IQ, `OPENCLAW_QUICK_RESEARCH_BUDGET`) → Hub decision (`q` inside a narrow `[p_low, p_high]` → ledger `research_skip no_divergence`, else escalate) → FULL stage (`OPENCLAW_FULL_RESEARCH_BUDGET`, markets in parallel via `OPENCLAW_RESEARCH_WORKERS`): `briefer` (4 fixed-slot queries) → Hub evidence fetch (`OPENCLAW_EVIDENCE_SOURCE` `aiq|web|ab`, default `ab` = stable per-market A/B on sha256 parity; A-IQ failure falls back to the price-blind `web_searcher` spoke, whose output the Hub sanitizes of market/odds lines; 24h query caches, same-event bundle reuse; ledger rows carry `evidence_source`, `stats.md` breaks Brier down by it) → `deep_researcher` (1 round + 1 follow-up, forced synthesis) × 3 independent samples → median `p`/`p_low`/`p_high`; sample spread > 0.15 or `|p - q| > 0.40` (defaults of the Overseer-tunable knobs; the gap is wider for paper trading) → `research_skip` → `02_Active_Research/`. Research is refreshed after a TTL or an 8-point price move. `OPENCLAW_RESEARCH_TICK_BUDGET_SEC` caps wall-clock per tick.
4. Execution is deterministic Hub code (no agent): `compute_allocation` (`skills/calculate-trade-allocation`: λ-shrink toward `q`, ¼-Kelly; while `PAPER_TRADE_MODE` the looser `PAPER_LAMBDA`/`PAPER_MIN_EDGE` (0.5/0.03) apply via `effective_lambda()`/`effective_min_edge()`, live keeps `LAMBDA`/`MIN_EDGE` (0.3/0.04); Israeli tax is `TAX_MODE = "annual_offset"`: 25% of NET gains per calendar year, so Kelly odds are pre-tax and `stats.md` reports pre-tax / tax / after-tax; `"per_trade"` restores the legacy per-win tax) with the real CLOB asks as `q_yes_ask`/`q_no_ask` and the order capped by ask depth within 2c (a failed quote skips the market for the tick) → in-process `execute_polymarket_trade` → `03_Trades/` + ledger `traded`/`below_edge` (idempotent via intent files).
5. Resolution (`polymarket_api.resolution`: Gamma `closed` + `outcomePrices`, also for gate-rejected ledger markets) → move to `04_Post_Mortems/` and run the Post-Mortem Analyst with structured inputs (p, range, entry, outcome, pre-tax per-bet P&L, ...); it returns a `failure_class` (`bad_info|bad_reasoning|resolution_misread|variance|correct`) stored in the post-mortem frontmatter and on a ledger `post_mortem` row.
6. Learning loop (Overseer), evidence-gated and bounded: auto-reverts a directives version whose Brier is worse than the previous one's by > 0.02 (>= 30 scored markets each), else runs only with >= `OVERSEER_MIN_NEW_RESOLVED` (30) new ledger resolutions since the last run and >= 24h. Input: `stats.md`, failure-class counts, <= 15 post-mortems, directives, `BOUNDS` table. The `filters:` block in `00_System/active_directives.md` holds ONLY the knobs in `config.trading_constants.BOUNDS` (gate price band, days window, `research_max_quick_width`, `research_max_sample_spread`, `research_max_q_gap`; read via `extract_filter_directives` by `gate.py`/Phase 3). `ObsidianManager.write_directives` rejects the whole doc on out-of-range, > 15% step, > 2 knobs, 3-version cooldown or unknown key; every success bumps the integer `version` and archives `00_System/directives_history/v{N}.md`. Sizing (λ, Kelly, caps) is off-limits to the Overseer.

**Spokes (`agents_blueprint/<role>/`)** — each role has `AGENTS.md` (prompt), `TOOLS.md`, and `agent.yaml` (`openclaw_agent_id`, tool allowlist, `input_schema` and `output_schema`). `agents_blueprint/loader.py` builds `AGENTS` and turns the YAML schemas into Pydantic models. `runner.spawn_agent` validates the input against those models before invoking the agent. Agents never write files and never do math. They call deterministic skills in `skills/` and return JSON or Markdown, which the Hub parses (`parse.py`, `agent_outputs.py`) and validates (`schema_validation.py`).

**Runner modes** (`OPENCLAW_ORCHESTRATOR_MODE`): `stub` (default, canned schema-valid responses), `stub_error` (forces the error path), and `live`. Unknown values raise an error and never fall back to stub silently.

**Vault I/O (`obsidian_utils.ObsidianManager`)** — the single gatekeeper for all writes. It uses Pydantic payload models and atomic writes. The vault root is `OPENCLAW_VAULT_PATH`, or the repo root by default, with subdirectories `Vault/00_System` … `Vault/04_Post_Mortems` (`config/trading_constants.VAULT_PATHS`). Tests should point it at a tmp dir.

**Error model: an in-place state machine with no error directory.** When an agent returns an error or parsing or validation fails, the Hub sets `status: inactive` and `error_log` on that market's own artifact (YAML frontmatter, or the JSON root for trades) and moves on to the next market. Inactive markets are skipped until `orchestrator.replay` clears the flags. Recent commits draw a deliberate line here: infra/transport failures are retried, content failures mark the market inactive, and soft gate failures back off (`next_check_at`) instead of being flagged. A failed trade call is deliberately *not* retried (the intent record stays). Keep these categories separate.

**Config:** trading and strategy constants (`F_MAX`, `LIQ_FRAC`, `FILTERS`, `PAPER_TRADE_MODE`, A-IQ settings, `RESEARCH_*` thresholds, Overseer `BOUNDS`) are in `config/trading_constants.py`. Env-driven tunables (`OPENCLAW_*`: timeouts, retries, `OPENCLAW_TOP_MARKETS`, `OPENCLAW_AUTO_REPLAY`, funnel budgets `OPENCLAW_GATE_BUDGET`/`OPENCLAW_TRIAGE_BUDGET`/`OPENCLAW_QUICK_RESEARCH_BUDGET`/`OPENCLAW_FULL_RESEARCH_BUDGET`, `OPENCLAW_RESEARCH_WORKERS`, `OPENCLAW_RESEARCH_TICK_BUDGET_SEC`, `OVERSEER_MIN_NEW_RESOLVED`) are in `orchestrator/config.py`.

## Testing notes

- `tests/orchestrator/conftest.py` autouse-fakes `polymarket_api.executable_quote` (asks follow the row under test; `QUOTE_HINTS` for direct Phase 4 rows) so phase tests never touch the network. `test_polymarket_api.py` fakes the HTTP session with real trimmed responses from `tests/orchestrator/fixtures/polymarket/`.
- Skill tests use fixtures under `tests/skills/fixtures/`.

## Knowledge graph (from `.cursor/rules/graphify.mdc`)

`graphify-out/` holds a code knowledge graph. For architecture questions, try `graphify query "<question>"` / `graphify path "<A>" "<B>"` / `graphify explain "<concept>"` before broad grepping, and read `graphify-out/GRAPH_REPORT.md` only for wide reviews. After changing code, run `graphify update .` (AST-only, no API cost).

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
