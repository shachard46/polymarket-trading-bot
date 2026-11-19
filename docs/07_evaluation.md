# 07 Evaluation: research review and backtest

Two tools measure whether research is good before enough trades resolve to show it in Brier. Both live in `scripts/`.

## 1. Rubric review (`scripts/review_research.py`)

Purpose: catch process failures (misread rules, missing cruxes, fabricated or stale evidence) that Brier needs many resolutions to reveal.

### Procedure

1. Build packets for the N most recent research notes (default 20):
   `python3 scripts/review_research.py packets /opt/pmbot/Vault -n 20 --gamma --format json -o packets.json`
   Each packet has: question, rules (Gamma with `--gamma`, plus the note's resolution checklist), cited evidence lines with URLs from the bundle, the note body, the bot's p / range / samples, the market price q (omit with `--blind`), and the pipeline tag (`crux_v1` or `legacy`).
2. Give each packet to a sonnet reviewer agent, one at a time. The reviewer must form its own p from the question, rules and evidence BEFORE reading the "Bot estimate" section.
3. The reviewer scores five checks:
   - **Resolution read:** does the note's reading of the rules (what counts as YES, source, deadline, polarity) match the rules text? Fail on a misread.
   - **Key cruxes covered:** list the 3-5 questions that decide this market (base rate or reference class, current state such as polls, scheduled catalysts, the case against). Which ones does the evidence address?
   - **URL spot-check:** open 3 cited URLs (prefer the ones behind the strongest claims). Is the claimed fact actually on the page? Any dead or unrelated link is a fail.
   - **Stale use:** does the note lean on evidence that is old or undated relative to `as_of` (labels `STALE` / `undated`) for a claim about the current state?
   - **Own-p gap:** reviewer's own p minus the bot's p. A gap above 0.15 needs an explanation of which side is wrong.
4. Grade:
   - **GOOD:** resolution read correct, all key cruxes covered, URLs verify, no stale reliance, own-p gap <= 0.10.
   - **OK:** minor problems: one crux missing, one weak or unverifiable URL, mild staleness, or gap 0.10 to 0.15.
   - **BAD:** any of: resolution misread, a decisive crux missing, a cited fact not on the page, a conclusion resting on stale evidence, or gap > 0.15 that the reviewer can justify from the evidence.
5. The reviewer outputs JSON `[{"market_id", "pipeline", "grade", "own_p", "bot_p", "notes"}]` (notes: one line naming the failed check). Save it and append:
   `python3 scripts/review_research.py record /opt/pmbot/Vault --verdicts verdicts.json`
   Rows are appended to `Vault/00_System/research_reviews.md` (table: reviewed_at, market_id, pipeline, grade, own_p, bot_p, notes).
6. Compare the GOOD/OK/BAD mix per pipeline tag. Reference baseline from the first review: 1 GOOD, 2 OK, 1 BAD.

The reviewer is a rubric aid, not a decision maker; the grades never feed sizing. Reviewing the reviewer: re-grade a random 20% yourself.

## 2. Backtest (`scripts/backtest.py`)

Runs the research function on recently resolved binary markets as if it were a date before resolution, then scores p against the outcome.

```
python3 scripts/backtest.py --dry-run                       # list markets, cutoffs, freshness; no cost
BRAVE_API_KEY=... python3 scripts/backtest.py --max-markets 10 --days-before 7
python3 scripts/backtest.py --research-fn legacy=pkg.mod:fn --research-fn crux_v1=pkg.mod:fn2
```

- Markets: Gamma `closed=true`, end date in the last `--days-back` (30) days, binary Yes/No with a clean 1/0 settle. A market created after its cutoff is skipped.
- Cutoff: `end_date - --days-before` days. `today` is forced to the cutoff and Brave `freshness` is the range `YYYY-MM-DDtoYYYY-MM-DD` ending at the cutoff (`--lookback-days`, default 180, before it).
- Research interface (thin): `research(row, today, search_freshness) -> {"p": float, ...}`; optional `pipeline` key overrides the tag. The default imports `orchestrator.phases.p3_research:backtest_research` lazily and wraps it in `brave_search.freshness_override`, so every Brave call, including those in worker threads, is date-limited. The FULL stage must expose that adapter; it must use `today` instead of the clock and must not show the model any price.
- Output: `logs/backtest/<timestamp>/results.json` and `report.md` with Brier and log loss per pipeline tag (always-0.5 reference: 0.25 and 0.693).
- Cost guard: `--max-markets` (default 10); each market costs a full research run per pipeline.
- Refuses to run without `BRAVE_API_KEY` (Gemini grounding cannot be date-limited) unless `--allow-leaky`; leaky runs are flagged in the report.

### Leakage caveats

Logged at start and written into every report: Brave page ages can be wrong or edited after publication; the research model's training data may contain the outcome; Gemini cannot be date-limited; markets are selected for having resolved. Ten markets is far too few to separate pipelines: use the result to find breakage and gross miscalibration, and compare pipelines only on identical markets.
