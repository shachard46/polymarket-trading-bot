# CLAUDE.md

Guidance for Claude Code (claude.ai/code) in this repository.

## Purpose and direction

The goal is a bot that makes money on Polymarket by finding markets where careful, price-blind research of public information disagrees with the market price, then betting small, Kelly-sized amounts on that disagreement. The current stage is **paper trading to collect evidence**: is the research calibrated (Brier score), and how much should we trust it versus the market (λ)? Live money comes only after the ledger shows it.

Principles, which should hold for any future change:
- **LLMs judge, code decides.** Models only read text and estimate probabilities. Gating, sizing, caps, tax, execution and maths are deterministic Hub code, so decisions are reproducible and testable. Agents that used to decide (Evaluator, Re-Evaluator, Executioner) were removed for this reason.
- **Price-blind research.** No model ever sees the market price `q`. Models given `q` anchor on it. Trust in the market is applied in code (the λ-shrink), where it can be measured.
- **Assume the market is usually right.** Cheap filters run before expensive research. Large disagreements are probable misreads, not big edges. Sizing is conservative: ¼-Kelly, shrink toward `q`, minimum edge, exposure caps.
- **Measure everything, learn slowly.** Every decision, including skips and rejects, goes to the decision ledger and is scored on resolution. `stats.md` reports Brier, calibration, P&L and a λ fit. The Overseer nudges a few bounded knobs, only with enough new resolutions, is auto-reverted if Brier worsens, and never touches sizing.
- **Measure before you filter.** A new filter starts as measurement: a shadow run, a triage shadow, an independent-evidence sample, or a record-only score. Hard-reject only extreme cases until resolved data justifies more.
- **Cost-aware.** LLM calls and evidence fetches are the main cost: per-tick budgets, caches, a quick stage before the full one.
- **Humans approve, code acts.** Approval flags put a human yes/no between a decision and money or knob changes. The concierge only relays Hub files and records votes: it never decides, filters or keeps preferences.
- **Fail safe, never silently.** Content errors mark the single market `inactive` and the loop moves on. Infra errors are retried. Unknown config values raise instead of falling back. Trades are never retried blindly. Measure-only runs never flag the real market; they ledger `shadow_failed` or an error field instead.

## Why each phase looks the way it does

1. **Ingest: direct Gamma/CLOB APIs.** The old scraper plus SQLite was a stale second copy. The public APIs give fresh prices, real order books and a YES/NO token mapping by label, never by position.
2. **Gate and triage: cheap before expensive, and wary of traps.**
   - A deterministic score prefers quiet markets (less attention, more mispricing) that resolve soon enough to score. Failing markets back off and retire.
   - One cheap price-blind triage call asks whether public research can plausibly beat this market at all (a coin flip or insider-driven question cannot). The answer does not change, so it is cached.
   - The tension: quiet markets have the most mispricing, but they are also where one actor can cheaply set a trap. So triage rates `manipulation_risk`, and the gate and an integrity check look for thin-volume moves, concentrated holders or wallets, wide spreads, rewritten rules and past disputes.
   - Measure before filtering: only extreme integrity cases reject, the rest is a record-only score, and triage rejects are sampled into a shadow run.
3. **Research: two stages, three samples, skip on doubt.**
   - The quick stage (model knowledge only) drops markets where we already agree with the price; a shadow run checks this does not discard edge.
   - Four fixed briefing queries target known LLM failures: misread rules, ignored base rates, stale knowledge, confirmation bias.
   - A median of three samples reduces noise; wide spread means unsure, so skip. A huge gap to `q` is likelier a misread than an edge, so skip too.
   - The evidence source is A/B tested because A-IQ is expensive and unproven; an extra sample on the other source measures evidence noise versus reasoning noise.
4. **Execution: deterministic sizing.** λ-shrink trusts our estimate only partly until the ledger fit says otherwise. An edge at the midpoint is not real, so size is capped at real ask depth. Paper mode loosens λ/edge only to collect scored bets faster. With `CONCIERGE_APPROVAL=1` a passing trade is parked as an offer and executed only on a yes vote, after re-quote and re-size; the edge must still clear.
5. **Resolution and post-mortem: classify why.** Every ledger market is resolved, including skipped and rejected ones, so filter costs are measurable. The post-mortem separates fixable process errors from variance. Open tags catch new failure modes, trusted only once they recur.
6. **Overseer: bounded, evidence-gated learning.** LLM self-tuning overfits noise, so it acts only after enough new resolutions, moves few knobs in small steps within hard bounds, and auto-reverts on worse Brier. With `CONCIERGE_APPROVE_DIRECTIVES=1` each knob change waits for a yes vote instead.

Open questions the project is working toward:
- Is research Brier better than the market's? What is the fitted λ?
- Is A-IQ worth its cost versus web search?
- Do the skip thresholds (spread, misread gap) throw away good bets?
- Does the quick filter discard edge? (quick-stage shadow)
- What do triage rejects cost? (triage shadow)
- How much noise comes from evidence versus reasoning? (independent-evidence sample)
- Do integrity/manipulation flags predict a less accurate market price?

Deferred work: `docs/TODO.md`.

## Commands

```bash
pip install -r requirements.txt            # orchestrator deps
python3 main.py                            # run the orchestrator loop (stub runner by default)
OPENCLAW_ORCHESTRATOR_MODE=live python3 main.py   # call real agents via the `openclaw agent` CLI
OPENCLAW_ORCHESTRATOR_MODE=direct ANTHROPIC_API_KEY=... python3 main.py   # call the Anthropic API directly, no OpenClaw
python3 -m orchestrator.discord_bot        # built-in Discord front-end (separate process)

python3 -m pytest tests                    # full suite
python3 -m pytest tests/orchestrator/test_parse.py::test_name   # single test

python3 -m orchestrator.replay --market-id 0xabc [--dry-run]   # clear `status: inactive` for one market
python3 -m orchestrator.replay --all [--dir filters]           # clear across vault dirs
python3 -m orchestrator.replay --directives 3                  # restore archived directives v3 as a NEW version

python3 skills/<skill>/run.py '<json>'     # invoke a skill directly (same entrypoint agents use)
OPENCLAW_AGENTS_ROOT=~/.openclaw/agents ./scripts/sync_openclaw_agent_workspace.sh <role|all>  # push agents_blueprint/ to OpenClaw
```

## Architecture

Hub-and-Spoke LLM trading pipeline. Specs in `docs/01_architecture.md` to `docs/06_concierge.md` (pipeline, sizing math, skill and agent I/O, concierge); keep them in sync with behavior.

**Hub (`orchestrator/`)** is the only component that spawns agents or touches disk. `loop.run_forever` runs `run_pipeline_tick` every `PIPELINE_INTERVAL_SEC` and the Overseer every `OVERSEER_INTERVAL_SEC`. `orchestrator/phases/` has one module per phase or stage (`p1_ingest`, `p2_routing`, `p3_quick`/`p3_full`/`p3_evidence`/`p3_queue`, `p4_execution`, `p5_resolution`, `p6_overseer`). Phase helpers: `p2_integrity` (integrity features, signal coverage), `p3_samples` (extra and independent samples, median aggregation), `p4_approval` (offers, inbox votes); `common`, `agent_call` and `research_state` are shared. `orchestrator/util.py` holds small coercion helpers. `runner_stubs.py` holds the deterministic stub responses for `stub` / `stub_error`. Tests must patch the defining submodule, not the package.

1. **Ingest.** Gamma API via `polymarket_api.py` (wrapped by `scraper.py`): open binary Yes/No markets in the days window with enough liquidity. `market_id` = condition id (`0x...`).
2. **Gate + triage + integrity.** `gate.py` (eligibility, score, backoff, arbitrage) ranks markets; top `OPENCLAW_GATE_BUDGET` go to the price-blind `triage` agent (cached per market/event forever); top `OPENCLAW_TRIAGE_BUDGET` researchable ones get `passed: true` in `01_Filters/`.
   - `status: retired`: triage rejects, triage `manipulation_risk: high`, 6 consecutive gate fails (`MAX_FAILS`).
   - Gate rejects `suspicious_move`, `concentrated`, `wide_spread` (fixed `MANIP`). Missing signals never reject. Holders: data-api `/v2/holders`, in-budget markets only, before triage. Basis: share of outstanding shares from `/v2/oi` (`holder_basis: supply`, top-1 0.25 / top-3 0.50), else share of top-20 holders (`top_n`, 0.50 / 0.75, record-only: `concentrated` rejects only on supply).
   - Integrity (`market_integrity.py`, in-budget, before triage): `integ_*` features over 1d/7d/30d from CLOB prices-history, data-api `/v2/trades`, CLOB book, Gamma. Hard rejects `ramp_thin`, `wallet_dominated`, `rules_changed`, `disputed_before` (fixed `INTEGRITY`); `integ_score` (0-1) and `integ_flags` are record-only, copied to later ledger rows.
   - Triage shadow: stable ~10% of triage rejects (`TRIAGE_SHADOW_RATE`, max 2/tick, last in Phase 3) get a measure-only quick estimate, ledger `triage_shadow`; no trade or status change.
   - Per tick a `signal_coverage` row (`market_id` `_tick`) counts missing gate signals; above `SIGNAL_COVERAGE_ALERT_FRAC` (0.5) it logs an error.
3. **Price-blind research.** QUICK (one `deep_researcher` call, no A-IQ, `OPENCLAW_QUICK_RESEARCH_BUDGET`) → `q` inside `[p_low, p_high]` → ledger `research_skip no_divergence`, else FULL (`OPENCLAW_FULL_RESEARCH_BUDGET`, parallel via `OPENCLAW_RESEARCH_WORKERS`): `briefer` (4 fixed-slot queries) → Hub evidence fetch → `deep_researcher` (1 round + 1 follow-up) × 3 samples → median `p`/`p_low`/`p_high` → `02_Active_Research/`.
   - Skips (`research_skip`): spread > 0.15 or `|p - q|` > 0.40 (knob defaults; gap wider in paper mode). Refresh after a TTL or 8-point price move; `OPENCLAW_RESEARCH_TICK_BUDGET_SEC` caps tick time.
   - Evidence: `OPENCLAW_EVIDENCE_SOURCE` `aiq|web|ab` (default `ab`: stable per-market A/B). A-IQ failure falls back to `web_searcher`; the Hub sanitizes odds lines. 24h query caches. `stats.md` splits Brier by `evidence_source`.
   - Quick shadow: stable ~10% of quick `no_divergence` skips (`SHADOW_RATE`, max `SHADOW_MAX_PER_TICK` 2, only after the FULL budget) also run FULL: no report, state change or trade. Ledger `shadow_research`; `stats.md` 'Shadow: quick vs full vs market'. Errors ledger `shadow_failed`.
   - Independent sample: stable ~20% of non-shadow FULL markets (`INDEP_SAMPLE_RATE`) get one extra sample on the other source (aiq <-> web). Fields `indep_p`, `indep_p_low`, `indep_p_high`, `indep_source`, `indep_gap` (vs median p), `indep_error`, copied to ledger rows. Never in median, skips or sizing. `stats.md` 'Independent evidence sample'.
4. **Execution** (Hub code, no agent): `compute_allocation` (`skills/calculate-trade-allocation`): λ-shrink toward `q`, ¼-Kelly. `PAPER_TRADE_MODE` uses `PAPER_LAMBDA`/`PAPER_MIN_EDGE` (0.5/0.03) via `effective_lambda()`/`effective_min_edge()`; live `LAMBDA`/`MIN_EDGE` (0.3/0.04). Tax is annual offset only: 25% of NET annual gains; Kelly is pre-tax. Real CLOB asks, order capped by ask depth within 2c (failed quote skips the tick) → `execute_polymarket_trade` → `03_Trades/` + ledger `traded`/`below_edge` (idempotent intent files). Approval mode adds `offer_pending` → `offer_approved` / `offer_stale` / `offer_declined` / `offer_expired` (+ `user_vote`); `stats.md` 'Offers & votes'. Ledger rows carry `runner_backend`. Research rows also carry `model` (direct adds `provider`; live takes `OPENCLAW_MODEL_LABEL`). `stats.md` has 'Brier by model' and 'Brier by runner backend'.
5. **Resolution** (Gamma `closed` + `outcomePrices`, also for gate-rejected ledger markets) → `04_Post_Mortems/`; `failure_class` (`bad_info|bad_reasoning|resolution_misread|variance|correct`), ledger `post_mortem`. Open tags `new:<slug>` (+ `failure_tag_definition`) are counted in `00_System/failure_tags.json`, established at `FAILURE_TAG_MIN_COUNT` (3).
6. **Overseer**: auto-reverts a directives version whose Brier is worse by > 0.02 (>= 30 scored markets each); otherwise runs with >= `OVERSEER_MIN_NEW_RESOLVED` (30) new resolutions and >= 24h since last run.
   - Inputs: `stats.md` (Signal coverage, Triage shadow, Independent evidence sample, Gate flags vs market calibration, Shadow: quick vs full vs market), Market integrity (report-only), failure-class counts (established `new:` tags included), `candidate_failure_tags` (not evidence), <= 15 post-mortems, directives, `BOUNDS`.
   - Only the quick-vs-full shadow can justify a knob change (lower `research_max_quick_width`); the rest is report-only.
   - `filters:` in `00_System/active_directives.md` holds ONLY `BOUNDS` knobs (gate price band, days window, `research_max_quick_width`, `research_max_sample_spread`, `research_max_q_gap`). `ObsidianManager.write_directives` enforces range, step, knob-count and cooldown limits, versions and archives each change. Sizing is off-limits.

**Measurement (`orchestrator/stats/`)**: pure functions over ledger rows. `calc` (Brier, log-loss, bootstrap CI), `groups` (row groupings), `pnl` (P&L, annual-offset tax, offers), `render` (`stats.md`).

**Spokes (`agents_blueprint/<role>/`)**: `AGENTS.md` (prompt), `TOOLS.md`, `agent.yaml` (agent id, tool allowlist, input/output schemas); `loader.py` builds Pydantic models from the YAML; `runner.spawn_agent` validates input. Agents never write files or do math; the Hub parses (`parse.py`, `agent_outputs.py`) and validates (`schema_validation.py`) their output.

**Runner modes** (`OPENCLAW_ORCHESTRATOR_MODE`): `stub` (default, deterministic), `stub_error` (injected failures), `live` (`openclaw agent` CLI, needs the gateway), `direct` (Messages API via `DIRECT_PROVIDER`, see below). Unknown values raise.
- `direct`: stateless, one Messages call per spawn. System prompt = the role's AGENTS.md (prompt-cached); user message = the same live prompt as `live`. Only `web_searcher` gets web search/fetch tools. Model per role: `DIRECT_MODEL_<ROLE>` (defaults: triage `claude-haiku-5-5`, overseer `claude-opus-5-5`, others `claude-sonnet-5-5`). `DIRECT_MAX_TOKENS` (16000), `DIRECT_WEB_MAX_USES` (8, per web_searcher call). Retries reuse `OPENCLAW_AGENT_MAX_ATTEMPTS` and backoff.
- Provider `DIRECT_PROVIDER`: `anthropic` (default, `ANTHROPIC_API_KEY`) or `openrouter` (`OPENROUTER_API_KEY`; `OPENROUTER_BASE_URL` default `https://openrouter.ai/api/v1`; optional `OPENROUTER_HTTP_REFERER`, `OPENROUTER_APP_TITLE`). Openrouter has no built-in role models: with no `DIRECT_MODEL_<ROLE>` it needs `DIRECT_MODEL_DEFAULT`, else `require_api_key` fails fast at startup. Model ids are never guessed.
- Session nonce: `runner.set_run_nonce(tick_id)` each tick, mixed into OpenClaw session keys (`orch-<nonce>-<market>`). Before, a market re-researched on a later tick reused its transcript and saw its own earlier estimate, so samples were not independent. No transcript is reused across ticks now.

**Hub outbox and concierge.** `orchestrator/notify.py` writes `06_Concierge/outbox/`: offers built from the Phase 4 trade record, a daily digest, and directive proposals (approval on). Votes in `06_Concierge/inbox/` are read by `p4_approval.process_inbox` and `p6_overseer.process_directive_votes`, polled every `CONCIERGE_POLL_SEC` between ticks when a flag is on. The **concierge** (`agents_blueprint/concierge/`: OpenClaw + Discord, `hub_spawned: false`) posts outbox files, writes votes, and answers questions from `stats.md`, `02_Active_Research` and the ledger, quoting only numbers found there. Setup, file formats and flag behaviour: `docs/06_concierge.md`. Notification never raises and never breaks a tick. Built-in Discord bot: `python3 -m orchestrator.discord_bot`, a separate process (never imported by the Hub) on the vault machine. Same outbox/inbox as the concierge, shared `outbox/_posted.json`. Env: `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, `DISCORD_OWNER_ID` (required); `CONCIERGE_TZ`, `CONCIERGE_QUIET_HOURS`, `CONCIERGE_POLL_SEC` (optional). Owner Q&A uses the direct backend (role `concierge`). Setup: `docs/06_concierge.md` section 5.

**Vault I/O (`obsidian_utils/` package)** is the single gatekeeper for writes: `manager.py` (`ObsidianManager`) validates through `schemas.py` (Pydantic payload models, atomic writers); `research_bundle.py` and `directives.py` are its mixins. Vault root: `OPENCLAW_VAULT_PATH`, else repo root; dirs `Vault/00_System` … `Vault/04_Post_Mortems`, `Vault/06_Concierge` (`VAULT_PATHS`; `outbox/` Hub→user, `inbox/` user→Hub, `_done/`, `_pending/`, `_posted.json`). Tests use a tmp dir.

**Error model: in-place state machine, no error directory.** On an agent, parse or validation error the Hub sets `status: inactive` and `error_log` on that market's own artifact and moves on. Inactive markets are skipped until `orchestrator.replay` clears the flags. Infra failures are retried, content failures mark inactive, soft gate failures back off (`next_check_at`), a failed trade call is not retried. Keep these separate.

**Config.** Strategy constants are in `config/trading_constants.py`: `F_MAX`, `LIQ_FRAC`, `FILTERS`, `PAPER_TRADE_MODE`, A-IQ settings, `RESEARCH_*` thresholds, and the Overseer `BOUNDS` (the only Overseer-tunable knobs). These groups are fixed, not Overseer knobs: `MANIP`, `INTEGRITY`, `SHADOW_*`, `TRIAGE_SHADOW_*`, `INDEP_SAMPLE_RATE`, `SIGNAL_COVERAGE_ALERT_FRAC`, `FAILURE_TAG_MIN_COUNT`. Env tunables (`OPENCLAW_*`, `OVERSEER_MIN_NEW_RESOLVED`: timeouts, retries, budgets, workers) are in `orchestrator/config.py`. Concierge env: `CONCIERGE_APPROVAL` (0; 1 = offers need a yes vote), `CONCIERGE_APPROVE_DIRECTIVES` (0; 1 = Overseer changes need a yes vote), `CONCIERGE_POLL_SEC` (300), `CONCIERGE_OFFER_TTL_SEC` (14400), `CONCIERGE_DIRECTIVE_TTL_SEC` (172800), `DISCORD_WEBHOOK_URL` (optional push; failures logged, never raised). Direct env: see Runner modes. Discord bot env: see Hub outbox and concierge.

## Testing notes

- `tests/orchestrator/conftest.py` has autouse fakes so phase tests never touch the network: `polymarket_api.executable_quote` (asks follow the row under test; `QUOTE_HINTS` for direct Phase 4 rows), `fetch_holder_concentration` (returns None), the three integrity fetchers `fetch_price_history`, `fetch_trades`, `fetch_book` (return None), and the evidence source (`OPENCLAW_EVIDENCE_SOURCE=aiq`). Tests that need real behavior override them.
- `test_polymarket_api.py` fakes HTTP with fixtures in `tests/orchestrator/fixtures/polymarket/`; skill tests use `tests/skills/fixtures/`.

## Knowledge graph (graphify)

`graphify-out/` holds a code knowledge graph. For codebase questions, run `graphify query "<question>"` first (`graphify path "<A>" "<B>"` for relationships, `graphify explain "<concept>"` for one concept) before broad grepping; `graphify-out/GRAPH_REPORT.md` only for wide reviews. After changing code, run `graphify update .` (AST-only, no API cost).
