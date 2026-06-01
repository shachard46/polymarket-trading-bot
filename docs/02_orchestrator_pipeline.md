# Orchestrator Pipeline (Strict State Flow)

This document defines _what_ happens and _where_ data moves. Do not implement agent prompting here.

## 1. Data Ingestion

- **Action:** Orchestrator queries `polymarket-scraper` local SQLite DB for high-delta/new markets.
- **State:** Generates `target_market_ids: list[str]`.

## 2. Quantitative Routing

- **Action:** Orchestrator iterates over `target_market_ids` (edge refresh only runs when Phase 1 includes the market — not on every tick).
  - If an **open** trade log in `/03_Trades/` shows a **bet** (not edge-disqualified: `below_edge_threshold` is false, or legacy `allocation_usd` > 0 with no error), **skip** quantitative routing for that market on this tick.
  - Otherwise: pass `market_id` and parsed `filter_directives` from `active_directives.md` to **Evaluator** (no active research) or **Re-Evaluator** (`review_kind: quantitative` when active research exists and the trade log is not edge-only disqualified). The agent calls `evaluate_market_metrics`, which loads trends internally via `poly-scan`.
  - If active research exists and the open trade log is **edge-disqualified** (`below_edge_threshold` true, or legacy zero allocation with no error), spawn **Re-Evaluator** with `review_kind: edge_research_refresh`, prior filter log, research markdown, and trade JSON. On `retry_deep_research`, update `/01_Filters/{market_id}.md` with new quantitative data and set **`pending_edge_refresh: true`** in frontmatter. Phase 2 does **not** run the Deep Researcher.
- **State:** Passing evaluations are written to `/01_Filters/`. Failures set `status: inactive` and `error_log` on the filter file in place.

## 3. Qualitative Pipeline (Decoupled)

- **Action:** Phase 3 scans `/01_Filters/` only (does not poll `/03_Trades/` for edge refresh). A market is eligible if it is not `status: inactive` and **either**:
  - **A)** `passed: true` and no error-free file in `/02_Active_Research/` → Briefer → Deep Researcher → write active research.
  - **B)** `pending_edge_refresh: true` and `edge_research_refresh_count` in active research is below the cap → Deep Researcher only (overwrite active), increment count, strip `pending_edge_refresh` from the filter file.
- Queue is sorted by `confidence_multiplier` and capped with `OPENCLAW_TOP_MARKETS`.

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

1. Scans `/01_Filters/`, `/02_Active_Research/`, and `/03_Trades/` for `status: inactive`.
2. Strips `status` and `error_log` from matching files in place.
3. Does not immediately re-run agents; the next pipeline tick picks up restored artifacts organically.

**Re-entry nuance:**

- Cleared **trade logs** in `03_Trades/` are picked up by Phase 5 (`iter_open_trades`).
- Cleared **filter / active research** artifacts are reused when the scraper returns that `market_id` in Phase 1–2 or when Phase 3's filter scan finds eligible work.
