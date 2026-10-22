# Orchestrator Pipeline (Strict State Flow)

This document defines _what_ happens and _where_ data moves. Do not implement agent prompting here.

## 1. Data Ingestion

- **Action:** Orchestrator queries `polymarket-scraper` local SQLite DB for high-delta/new markets (`OPENCLAW_INGEST_LIMIT`, default 100).
- **State:** Generates `target_market_ids: list[str]`.

## 2. Gate + Triage (Deterministic Funnel)

The strategy is research-edge, not momentum, so Phase 2 is code (`orchestrator/gate.py`) plus one cheap LLM step (`triage`). Funnel per tick: ingest (100) -> **gate** (eligible, ranked, top `OPENCLAW_GATE_BUDGET`, default 15, go to triage) -> **triage** (top `OPENCLAW_TRIAGE_BUDGET`, default 8, researchable markets become Phase 3 candidates) -> research (`OPENCLAW_QUICK_RESEARCH_BUDGET` 8 quick passes / `OPENCLAW_FULL_RESEARCH_BUDGET` 3 full passes per tick, applied in Phase 3).

- **Skip silently** (no state change) when: `/01_Filters/{market_id}.md` has `status: inactive` (until `orchestrator.replay`) or `status: retired`; `next_check_at` is still in the future (backoff); or an open trade log shows a bet. A market with active research is not re-gated: Phase 2 only checks it for a price-move refresh (below).
- **Arbitrage:** `yes_price + no_price < 0.98` with both in (0, 1) writes a ledger `arb_opportunity` row (paper only, deduplicated) and the market is not researched.
- **Eligibility** (`gate.eligibility` returns `(ok, reason)`): implied price `q` in [0.05, 0.95]; `MIN_DAYS_TO_RESOLUTION` (7) <= days to resolution <= `MAX_DAYS_TO_RESOLUTION` (60); liquidity >= `MIN_ORDER_USD / LIQ_FRAC` (100 USD); not closed. Reason codes: `closed`, `no_price`, `price_extreme`, `no_end_date`, `too_soon`, `too_far`, `illiquid`. A failure writes ledger `gate_reject` (once per state change) and applies backoff.
- **Backoff state** lives in the filter frontmatter: `fail_count`, `next_check_at`, `last_q`. After the n-th consecutive failure the next check is `5h * 2^(n-1)` away, capped at 7 days. `fail_count` resets to 0 when `|q - last_q| > 0.05` or when the market passes. After 6 consecutive failures the market is `status: retired` (distinct from `inactive`, which means an error; retired is a deliberate, final "not worth looking at").
- **Ranking** (`gate.score`): `0.5*attention + 0.3*time + 0.2*price + bonus`. attention = `1/(1 + (volume/liquidity)/20)` (quiet markets first), time = 1.0 for 14-45 days (ramps to 0.4 at the 7/60 day edges), price = `1 - 2|q - 0.5|`, bonus = 0.04 per fired momentum signal (volume_shock, breakout, info_drift; from the `evaluate_market_metrics` signal bundle, fetch failure tolerated), capped at 0.10. Momentum never dominates; it is a "recheck now" nudge.
- **Triage** (agent `triage`, no price in its input): is public-information research plausibly better than a liquid market? The verdict `{researchable, category, reason, triaged_at}` is cached forever in the filter frontmatter (`triage:`) and shared across markets with the same `event_id` (one agent call per event, also across ticks). `researchable: false` writes ledger `triage_reject` and sets `status: retired`. Triage agent errors follow the usual rule: `status: inactive` + `error_log`.
- **State:** Every evaluated market is written to `/01_Filters/{market_id}.md` with `passed`, `score` (mirrored in `confidence_multiplier`), `trigger`, `details`, `event_id`, `signal_bundle`, backoff fields, and `triage`. `passed: true` is set only for the top `OPENCLAW_TRIAGE_BUDGET` researchable markets; the rest carry `trigger: over_gate_budget` / `over_triage_budget` and are re-evaluated next tick (their triage is cached, so this is cheap). Failed markets are **not** sent to the Overseer; macro-learning uses `/04_Post_Mortems/` only.

## 3. Research Pipeline (Decoupled, price-blind)

**Key principle: no model ever sees the market price `q`.** Models given `q` anchor on it (or are far off it when they do not); the anchoring lives in the Hub instead: the lambda-shrink toward `q` inside `compute_allocation` (`docs/03_strategy_math.md`), whose lambda is measured and refit from the ledger. Briefer, Deep Researcher and Triage payloads contain market text only (`title`, `description`, `resolution_source`, `end_date`, `category`, `today`); `q`, `yes_price` and friends are Hub-side. `q_at_research` is stored in the research frontmatter for the refresh trigger only.

Phase 3 scans `/01_Filters/` (`passed: true`, not `inactive`/`retired`) and processes candidates in gate-score order (`confidence_multiplier`, capped by `OPENCLAW_TOP_MARKETS`). A candidate is `initial` (no error-free research and no still-valid earlier attempt) or `refresh` (see below).

1. **QUICK stage** (up to `OPENCLAW_QUICK_RESEARCH_BUDGET` markets per tick): one `deep_researcher` call with `mode: "quick"`, **no A-IQ bundle** (`research_bundle: []`; only the market text, the resolution rules and the model's knowledge), forced `status: complete`, one sample. The Hub reads `q = select_q(fresh market row)`:
   - `q` inside `[p_low, p_high]` and `p_high - p_low <= 0.30` -> ledger `research_skip` reason `no_divergence` (stores `p`, `p_low`, `p_high`, `q`); no further work.
   - otherwise **escalate** to the FULL stage. If the FULL budget is already spent, the escalation is remembered (`research_state.outcome: escalate` in the filter record) and the market goes straight to FULL next tick without a second quick call.
2. **FULL stage** (up to `OPENCLAW_FULL_RESEARCH_BUDGET` markets per tick, taken in score order so the selection does not favour large disagreements): `briefer` -> Hub A-IQ fetch -> `deep_researcher`. The Briefer emits exactly 4 fixed-slot queries, each containing `as of {today}`: (1) resolution criteria and polarity (does YES equal the title event?), (2) reference class / base rate, (3) current state as of today, (4) strongest disconfirming evidence. The Deep Researcher runs at most **one round plus one follow-up round** (`MAX_RESEARCH_ITERATIONS = 2`; follow-up queries are fetched in between), then a forced-synthesis override. **Three independent samples** (separate `session_tag`s `s1`..`s3`, hence separate agent sessions) are taken on the same final bundle: the lead sample does the iterative loop, samples 2 and 3 are single forced-synthesis calls on its bundle. Aggregation: `p`, `p_low` and `p_high` are each the **median** across samples (this keeps `p_low <= p <= p_high`; the markdown body written is the sample closest to the median). Then:
   - `max(p) - min(p) > 0.15` across samples -> ledger `research_skip` `sample_disagreement`.
   - `|p_median - q| > 0.30` -> ledger `research_skip` `probable_misread` (the row carries `q`, `p`, `p_low`, `p_high`, `samples`, `spread`, `confidence`).
   - otherwise write `/02_Active_Research/{market_id}.md` with `estimated_p` (= median `p`), `p_low`, `p_high`, `confidence`, `as_of`, `resolution_checklist`, `key_assumptions`, `q_at_research`, `researched_at`, `end_date`, `samples`.
   Every attempt is recorded in the filter record (`research_state`: stage, outcome, p/range, q, time, `ttl_hours`). An unchanged, unexpired skip (same outcome, `q` moved <= 8 points, younger than the TTL) is not retried.
3. **A-IQ efficiency.** FULL-stage markets run **concurrently** (`ThreadPoolExecutor`, `OPENCLAW_RESEARCH_WORKERS`, default 3; vault writes are atomic per file, the ledger append is serialised). A-IQ results are cached for 24h in `/02_Active_Research/aiq_cache.json` keyed by the normalised query (lowercased, whitespace collapsed); error and empty results are never cached. A per-tick wall-clock budget `OPENCLAW_RESEARCH_TICK_BUDGET_SEC` (default 1800) stops new markets from starting once exceeded (running ones finish).
4. **Event grouping.** FULL candidates sharing an `event_id` run Briefer + A-IQ once (the first market); the others reuse that bundle. The Deep Researcher still runs per market. Each market counts against the FULL budget.
5. **Refresh.** Error-free research is re-queued for FULL (no QUICK) when it is older than `TTL = max(48h, days_to_resolution * 24h / 10)`, or when `|q - q_at_research| > 0.08` (checked in Phase 2 against the fresh row of each ingested market, which sets `refresh_reason: price_move` in the filter record). Markets holding an open bet, or no longer eligible per `gate.eligibility`, are not refreshed. A refresh attempt that ends in a skip leaves the old research in place and is not retried until its own cooldown lapses.

- **Hub I/O:** A-IQ results accumulate in `/02_Active_Research/research_bundles/{market_id}.json`. If that file already has usable rows (e.g. after `replay`), the initial FULL run skips the Query Planner and resumes synthesis; a refresh clears and rebuilds the bundle. Per-query `research_data` is capped at 4000 characters; the A-IQ poll timeout is `AIQ_TIMEOUT_SEC`.
- Agent errors / parse or validation failures flag the market `status: inactive` as before (a dropped extra sample does not; fewer than 2 valid samples does).

## 4. Execution (deterministic)

No agent is involved. For each market in this tick's researched list plus a vault scan of active research without a trade log or intent (the intent-record idempotency is unchanged):

- `compute_allocation(vault, p, q, market_data, p_low, p_high)` (runs `skills/calculate-trade-allocation`: shrink toward `q`, tax-aware quarter-Kelly, YES/NO side, caps).
- Allocation 0 (below the edge threshold or under the minimum order) -> trade log with `allocation_usd: 0`, `below_edge_threshold: true`, ledger `below_edge`. The skill is not called.
- Otherwise write an intent record, call `execute_polymarket_trade(market_id, outcome=side, amount=allocation_usd, price)` in-process, then write `/03_Trades/{market_id}.json` (`side`, `price`, `shares`, `p`, `p_low`, `p_high`, `q_entry`, `allocation_usd`, `executed = success and not paper`, `transaction_hash`), clear the intent and ledger `traded`. A failed trade call leaves the intent in place and flags the market inactive (never retried automatically). An edge-disqualified trade log may be re-executed after a fresh research refresh.

## 5. Market Resolution & Post-Mortem (Async/Scheduled)

- **Action:** Orchestrator queries `polymarket-scraper` for recently closed markets that exist in `/03_Trades/`.
- **State Management:** Orchestrator moves the market's file from `/02_Active_Research/` to `/04_Post_Mortems/`.
- **Action:** Spawn **Post-Mortem Analyst** with structured inputs (`p`, `p_low`, `p_high`, `q_entry`, `side`, `price`, `size_usd`, `outcome_yes`, after-tax `realised_pnl_usd` via `stats.bet_pnl`, triage `category`, `gate_score`, `research_confidence`, `resolution_checklist`) plus the research markdown, trade log and resolution data. The analyst separates decision quality from outcome and returns a `failure_class` in `{bad_info, bad_reasoning, resolution_misread, variance, correct}`. The Orchestrator appends the paragraph under `## Post-Mortem`, stores `failure_class` in the post-mortem frontmatter, and ledgers a `post_mortem` row (`failure_class`, `outcome_yes`, `pnl_usd`, ...). An invalid class is a failed attempt (retried, capped like any post-mortem failure).

## 6. Macro-Learning Loop (Every 24h, evidence-gated)

Phase 6 is a bounded, versioned, reversible tuner. The Overseer may edit the Research Protocol text and at most 2 gate/research knobs per run; it can never touch sizing (lambda, Kelly fraction, caps).

1. **Auto-revert first.** Group ledger decisions by `directive_version` (`stats.brier_by_directive_version`). If the current version has >= 30 resolved scored markets, so does the previous one, and the current model Brier is worse by more than 0.02, restore the previous version as a NEW version (`restored_from`, `restore_note` in the frontmatter), log loudly at ERROR, set `last_overseer_at`, and skip the Overseer. A version that is itself a restore is never auto-reverted.
2. **Gate.** Run only if >= `OVERSEER_MIN_NEW_RESOLVED` (default 30) ledger `resolution` rows exist beyond `last_overseer_ledger_count`, and `last_overseer_at` is older than `OVERSEER_INTERVAL_SEC` (24h). Both live in the directives frontmatter; `loop.run_forever` also keeps the 24h cadence.
3. **Input.** `stats.md` (rewritten just before), `failure_class_counts` (latest `post_mortem` row per market), up to 15 recent post-mortems (5 KB each), the current directives and the `bounds` table (`knob, current, default, min, max, max_step_frac, last_changed_version, locked`).
4. **Validation (`ObsidianManager.write_directives`).** The WHOLE proposal is rejected (prior directives kept, error logged, nothing ledgered, no version) if: the structure is invalid; the `filters:` block has an unknown key or a non-numeric value; a knob is outside `BOUNDS`; a knob moves more than 15% of its current value (integer knobs may move by 1); more than 2 knobs change; or a knob changed fewer than 3 versions ago (`knob_last_changed` in the frontmatter). An omitted knob counts as its default.
5. **Versioning.** The Hub owns the frontmatter: `version` is a monotonic integer (seed = 1; legacy `"0.1-seed"` migrates to 1), incremented by every successful write; each version is archived to `00_System/directives_history/v{N}.md` (plus the `.bak` of the previous file). A proposal that changes neither a knob nor the body is a no-op: no new version, only `last_overseer_*` is updated. Ledger rows stamp the integer version.
6. **Rollback.** `python -m orchestrator.replay --directives N` restores archived version N as version `latest + 1` (bounds are not enforced; knobs that differ restart their cooldown).

### Tunable knobs (`config.trading_constants.BOUNDS`, `filters:` block in `active_directives.md`)

| knob | default | min | max | read by |
|---|---|---|---|---|
| `gate_min_q` / `gate_max_q` | 0.05 / 0.95 | 0.03-0.15 / 0.85-0.97 | | `gate.eligibility` (`price_extreme`) |
| `min_days_to_resolution` / `max_days_to_resolution` | 7 / 60 | 3-21 / 30-120 | | `gate.eligibility` (`too_soon`/`too_far`), `gate.score` time ramp |
| `research_max_quick_width` | 0.30 | 0.15-0.45 | | `quick_decision` (`no_divergence`) |
| `research_max_sample_spread` | 0.15 | 0.08-0.25 | | FULL stage `sample_disagreement` |
| `research_max_q_gap` | 0.30 | 0.20-0.45 | | FULL stage `probable_misread` |

Each knob falls back to the constant of the same meaning when the directives are missing or unparsable (`extract_filter_directives`). The legacy signal-bundle thresholds (`volume_shock_ma_multiplier`, ...) stay in `FILTERS` but are not Overseer-tunable.

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
python -m orchestrator.replay --directives 3   # restore directives v3 as a new version
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
