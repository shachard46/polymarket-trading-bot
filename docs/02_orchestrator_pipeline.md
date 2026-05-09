# Orchestrator Pipeline (Strict State Flow)

This document defines _what_ happens and _where_ data moves. Do not implement agent prompting here.

## 1. Data Ingestion

- **Action:** Orchestrator queries `polymarket-scraper` local SQLite DB for high-delta/new markets.
- **State:** Generates `target_market_ids: list[str]`.

## 2. Quantitative Routing

- **Action:** Orchestrator iterates over `target_market_ids`.
  - For each market: load trend history from the scraper DB via `poly-scan get_market_trends <market_id> --limit N` (choose `N` to cover at least `breakout_time_window_hrs` and `low_liquidity_dead_window_hrs` from `FILTERS`). Reverse the newest-first result to oldest-first.
  - Pass `historic_market_data` to `evaluate_market_metrics`. Both **Evaluator** (new markets) and **Re-Evaluator** (markets already in `/02_Active_Research/`) use this same series-based input; deltas are computed inside the skill, not from Obsidian baselines.
- **State:** Filters output to `passed_markets: list[dict]`.

## 3. Qualitative Pipeline

- **Action:** For each market in `passed_markets`:
  1. Spawn **Briefer** $\rightarrow$ Save Context Summary string.
  2. Spawn **Deep Researcher** (inject Context Summary + `active_directives.md`) $\rightarrow$ Save Markdown report to `/02_Active_Research/`.
  3. Parse estimated probability ($p$) from the Markdown report.

## 4. Execution

- **Action:** Spawn **Executioner** (inject $p$ and live market JSON) $\rightarrow$ Save output to `/03_Trades/`.

## 5. Market Resolution & Post-Mortem (Async/Scheduled)

- **Action:** Orchestrator queries `polymarket-scraper` for recently closed markets that exist in `/03_Trades/`.
- **State Management:** Orchestrator moves the market's file from `/02_Active_Research/` to `/04_Post_Mortems/`.
- **Action:** Spawn **Post-Mortem Analyst** (inject original report, trade log, and resolution data) $\rightarrow$ Orchestrator appends the output to the `## Post-Mortem` section of the Markdown file.

## 6. Macro-Learning Loop (Every 24-48 Hours)

- **Action:** Orchestrator aggregates all updated files in `/04_Post_Mortems/`.
- **Action:** Spawn **Overseer** (inject aggregated post-mortems and current `/00_System/active_directives.md`).
- **State Management:** Orchestrator completely overwrites `/00_System/active_directives.md` with the Overseer's `new_directives_markdown` output.

## 7. Error Handling & The Dead Letter Queue

- **Action:** At any phase in the pipeline, if an agent's output contains a non-null `error` string, or if the Orchestrator fails to parse the agent's YAML/JSON output:
- **State Management:** The Orchestrator halts the current market's progression, moves any existing files for that market into `/Vault/05_Errors/`, and immediately logs the exception details in that file. The pipeline then seamlessly continues to the next market ID.
