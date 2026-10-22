# Orchestrator Pipeline (Strict State Flow)

This document defines _what_ happens and _where_ data moves. Do not implement agent prompting here.

## 1. Data Ingestion

- **Action:** Orchestrator queries `polymarket-scraper` local SQLite DB for high-delta/new markets (`OPENCLAW_INGEST_LIMIT`, default 100).
- **State:** Generates `target_market_ids: list[str]`.

## 2. Gate + Triage (Deterministic Funnel)

The strategy is research-edge, not momentum, so Phase 2 is code (`orchestrator/gate.py`) plus one cheap LLM step (`triage`). Funnel per tick: ingest (100) -> **gate** (eligible, ranked, top `OPENCLAW_GATE_BUDGET`, default 15, go to triage) -> **triage** (top `OPENCLAW_TRIAGE_BUDGET`, default 8, researchable markets become Phase 3 candidates) -> research (`OPENCLAW_QUICK_RESEARCH_BUDGET` 8 / `OPENCLAW_FULL_RESEARCH_BUDGET` 3, applied in Phase 3 from Wave 3).

- **Skip silently** (no state change) when: `/01_Filters/{market_id}.md` has `status: inactive` (until `orchestrator.replay`) or `status: retired`; `next_check_at` is still in the future (backoff); an open trade log shows a bet; or active research already exists (`# TODO(wave3)`: refresh of researched markets).
- **Arbitrage:** `yes_price + no_price < 0.98` with both in (0, 1) writes a ledger `arb_opportunity` row (paper only, deduplicated) and the market is not researched.
- **Eligibility** (`gate.eligibility` returns `(ok, reason)`): implied price `q` in [0.05, 0.95]; `MIN_DAYS_TO_RESOLUTION` (7) <= days to resolution <= `MAX_DAYS_TO_RESOLUTION` (60); liquidity >= `MIN_ORDER_USD / LIQ_FRAC` (100 USD); not closed. Reason codes: `closed`, `no_price`, `price_extreme`, `no_end_date`, `too_soon`, `too_far`, `illiquid`. A failure writes ledger `gate_reject` (once per state change) and applies backoff.
- **Backoff state** lives in the filter frontmatter: `fail_count`, `next_check_at`, `last_q`. After the n-th consecutive failure the next check is `5h * 2^(n-1)` away, capped at 7 days. `fail_count` resets to 0 when `|q - last_q| > 0.05` or when the market passes. After 6 consecutive failures the market is `status: retired` (distinct from `inactive`, which means an error; retired is a deliberate, final "not worth looking at").
- **Ranking** (`gate.score`): `0.5*attention + 0.3*time + 0.2*price + bonus`. attention = `1/(1 + (volume/liquidity)/20)` (quiet markets first), time = 1.0 for 14-45 days (ramps to 0.4 at the 7/60 day edges), price = `1 - 2|q - 0.5|`, bonus = 0.04 per fired momentum signal (volume_shock, breakout, info_drift; from the `evaluate_market_metrics` signal bundle, fetch failure tolerated), capped at 0.10. Momentum never dominates; it is a "recheck now" nudge.
- **Triage** (agent `triage`, no price in its input): is public-information research plausibly better than a liquid market? The verdict `{researchable, category, reason, triaged_at}` is cached forever in the filter frontmatter (`triage:`) and shared across markets with the same `event_id` (one agent call per event, also across ticks). `researchable: false` writes ledger `triage_reject` and sets `status: retired`. Triage agent errors follow the usual rule: `status: inactive` + `error_log`.
- **State:** Every evaluated market is written to `/01_Filters/{market_id}.md` with `passed`, `score` (mirrored in `confidence_multiplier`), `trigger`, `details`, `event_id`, `signal_bundle`, backoff fields, and `triage`. `passed: true` is set only for the top `OPENCLAW_TRIAGE_BUDGET` researchable markets; the rest carry `trigger: over_gate_budget` / `over_triage_budget` and are re-evaluated next tick (their triage is cached, so this is cheap). Failed markets are **not** sent to the Overseer; macro-learning uses `/04_Post_Mortems/` only.

## 3. Qualitative Pipeline (Decoupled)

- **Action:** Phase 3 scans `/01_Filters/` only. A market is eligible if it is not `status: inactive`/`retired`, has `passed: true`, and has no error-free file in `/02_Active_Research/` -> **Surgical Query Planner** (`briefer`) -> Hub parallel A-IQ fetch -> iterative **Forensic Fact Verifier** (`deep_researcher`) -> write active research. Edge-triggered refresh is not currently scheduled (redone in Wave 3).
- **Hub I/O:** A-IQ results accumulate in `/02_Active_Research/research_bundles/{market_id}.json`. If that file already has queries (e.g. after `replay`), Phase 3 skips the Query Planner and redundant fetches and resumes synthesis. Re-fetching the same query string replaces a prior row when the new result has `research_data` and the saved row had an error or empty data (successful rows are not overwritten by later failures). Per-query `research_data` is capped at 4000 characters; A-IQ poll timeout is `AIQ_TIMEOUT_SEC` from `config/trading_constants` (overridable via env).
- **Loop cap:** At most **four** Forensic Fact Verifier rounds; if still `needs_more_data`, a forced-synthesis override requires `status: complete` before writing `/02_Active_Research/{market_id}.md`.
- Queue is sorted by `confidence_multiplier` (the gate score) and capped with `OPENCLAW_TOP_MARKETS`.

## 4. Execution

- **Action:** Spawn **Executioner** (inject $p$ and live market JSON) $\rightarrow$ Save output to `/03_Trades/`.

## 5. Market Resolution & Post-Mortem (Async/Scheduled)

- **Action:** Orchestrator queries `polymarket-scraper` for recently closed markets that exist in `/03_Trades/`.
- **State Management:** Orchestrator moves the market's file from `/02_Active_Research/` to `/04_Post_Mortems/`.
- **Action:** Spawn **Post-Mortem Analyst** (inject original report, trade log, and resolution data) $\rightarrow$ Orchestrator appends the output to the `## Post-Mortem` section of the Markdown file.

## 6. Macro-Learning Loop (Every 24-48 Hours)

- **Action:** Orchestrator aggregates all updated files in `/04_Post_Mortems/`.
- **Action:** Spawn **Overseer** (inject aggregated post-mortems and current `/00_System/active_directives.md`).
- **State Management:** Orchestrator completely overwrites `/00_System/active_directives.md` with the Overseer's `new_directives_markdown` output. Overseer failures are logged; prior directives are retained.

## 7. Error Handling (In-Place State Machine)

- **Action:** At any phase, if an agent's output contains a non-null `error` string, or if the Orchestrator fails to parse or validate output:
- **State Management:** The Orchestrator halts progression for that `market_id`, sets **`status: inactive`** and **`error_log`** on the native artifact (YAML frontmatter for `/01_Filters/`, `/02_Active_Research/`, `/04_Post_Mortems/`; JSON root for `/03_Trades/`). Files are **not** moved to a separate errors directory. The pipeline continues with the next market.

### Recovery (`replay`)

When the root cause is fixed, clear inactive flags in place:

```bash
python -m orchestrator.replay --market-id 0xabc
python -m orchestrator.replay --all
python -m orchestrator.replay --all --dir filters
python -m orchestrator.replay --market-id 0xabc --dry-run
```

Or enable automatic replay at orchestrator startup:

```bash
export OPENCLAW_AUTO_REPLAY=1
```

**What replay does:**

1. Scans `/01_Filters/`, `/02_Active_Research/`, `/03_Trades/`, and `/04_Post_Mortems/` for `status: inactive`.
2. Strips `status` and `error_log` from matching files in place.
3. Does not immediately re-run agents; the next pipeline tick picks up restored artifacts organically.

**Re-entry nuance:**

- Cleared **trade logs** in `03_Trades/` are picked up by Phase 5 (`iter_open_trades`).
- Cleared **filter / active research** artifacts are reused when the scraper returns that `market_id` in Phase 1–2 or when Phase 3's filter scan finds eligible work.
