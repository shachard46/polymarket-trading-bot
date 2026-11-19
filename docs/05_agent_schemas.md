# Agent Personas and I/O Schemas

This document defines exactly _how_ agents behave and their strict JSON/YAML boundaries. All JSON outputs must handle explicit failure states.

Each role's [`agents_blueprint/<role>/agent.yaml`](../agents_blueprint/) declares `input_schema` and `output_schema`. In live mode the orchestrator builds the response hint from `output_schema` automatically. Optional `live_response_hint` (multiline string) appends extra rules when the schema alone is insufficient (e.g. Overseer markdown-in-JSON structure).

## 1. The Triage Agent (triage)

Phase 2's gate is deterministic code (`orchestrator/gate.py`, see `02_orchestrator_pipeline.md`); the only LLM step in Phase 2 is Triage. The former Evaluator and Re-Evaluator agents are removed.

- **System Prompt:** Decide whether public-information research by an LLM with web search could plausibly estimate the probability better than a liquid market. **No** for sports game lines/props, short-horizon crypto/asset price thresholds, pure chance, markets resolved by private or unknowable information, and markets whose answer is just a live data feed. **Yes** for politics/policy/legal/regulatory/corporate/scientific events with public evidence and clear resolution rules. The agent is never given the price. It also rates manipulation risk (see below).
- **Input Schema:** `{"market_id": "string", "title": "string", "description": "string", "resolution_source": "string | null", "category": "string | null", "end_date": "string | null", "today": "string"}`
- **Output Schema:**

```json
{
  "market_id": "string",
  "researchable": boolean,
  "category": "string",
  "reason": "string",
  "manipulation_risk": "low | medium | high",
  "manipulation_reason": "string",
  "error": "string | null"
}
```

- **Manipulation risk:** price-blind, text-only rating. `high` = vague/subjective source, a single small or obscure oracle/account, or an outcome one actor can directly cause or control; `medium` = some rule ambiguity or a concentrated-but-official source; `low` = clear rules and an authoritative independent source. The schema keeps it a plain string; the Hub validates the value.
- **Caching:** the Hub stores the verdict in the market's `01_Filters/` frontmatter (`triage:`) forever and reuses it for every market with the same `event_id`. `researchable: false` retires the market.

## 2. The question-led research roles (scout, briefer / architect, gap_analyzer, auditor)

The FULL stage (`crux_v1`, `docs/02_orchestrator_pipeline.md` section 3) uses four price-blind helper roles before the judges. None has tools; the Hub runs every search. Cheap roles are DeepSeek (`config/models.yaml`: `scout`, `briefer`, `gap_analyzer`, `auditor`).

### 2a. Scout (`scout`)

- **Input:** `{market_id, title, description, resolution_source, end_date, category, today, evidence: list[dict]}` (`evidence` = the rows of one broad pull, labelled). No price.
- **Output:** `{market_id, event, deciding_actors: [str], resolution_source: str|null, scheduled: [str], contested: [str], main_sources: [str], error}`. A map of the landscape: it must not estimate or lean.

### 2b. Crux Architect (`briefer`)

- **Input:** the market text fields plus `depth` (`"full"` | `"lite"`) and `scout` (the landscape dict above without `market_id`/`error`, or `null` when the scout failed). No price.
- **Output:**

```json
{
  "market_id": "string",
  "cruxes": [{"role": "mechanism|comparator|evidence|horizon|skeptic", "question": "string", "queries": ["string (1-2)"], "moves_p_if": "string"}],
  "error": "string | null"
}
```

5-8 cruxes (`lite`: 4-5). The Hub requires at least one `mechanism` (how and when it resolves), `comparator` (base rate / reference class), `horizon` (catalysts before `end_date`) and `skeptic` (the strongest case against the apparent leaning) crux and fills a missing role with a template query; every query carries `as of {today}` (appended if missing). An unknown `role` or a crux with 0 or more than 2 queries fails validation (retryable `format_validation_error`). The legacy output (exactly 4 `research_queries`, slot order mechanism / comparator / evidence / skeptic) is still accepted. `error` set means the Hub uses its template cruxes instead.

### 2c. Gap Analyzer (`gap_analyzer`)

- **Input:** `{market_id, title, end_date, today, cruxes: [{crux_id, role, question, moves_p_if, evidence: [labelled lines]}]}`.
- **Output:** `{market_id, assessments: [{crux_id, status: answered|unanswered|contradicted, missing}], follow_up_queries: [{crux_id, query}], error}`. The Hub runs **one** round, accepts at most 3 follow-up queries (unanswered cruxes only, no repeats) and stops there.

### 2d. Citation Auditor (`auditor`)

- **Input:** `{market_id, today, items: [{id: "E7", claim, url, page_text}]}` (at most 10 items: lines behind answered cruxes that have page text; the text is sanitized of market/odds mentions).
- **Output:** `{market_id, verdicts: [{id, supported: bool, reason}], error}`. `supported` only when the page text itself states the fact (same entity, number, date, direction). Unsupported lines are dropped by the Hub.

## 3. The Forensic Fact Verifier (deep_researcher)

The Deep Researcher is **price-blind**: its payload contains no `q`, `yes_price` or any other market price, and it must not be asked to anchor on or default to one. The Hub compares its output to `q` (see `02_orchestrator_pipeline.md` section 3).

- **System Prompt:** "**Forensic Fact Verifier**. Work resolution rules first (polarity!), then reference class, then verified current state as of `today`, then the strongest disconfirming evidence. Output a probability with an honest range. Inconclusive evidence means a wide range and low confidence - never a neutral default. In `mode: quick` you have no bundle and must answer from the market text and your knowledge; in `mode: full` you may use `needs_more_data` once (1-3 follow-up queries)."
- **Input Schema:** `{"market_id": "string", "title": "string", "description": "string", "resolution_source": "string | null", "end_date": "string | null", "category": "string | null", "today": "string", "mode": "string (quick|judge; full is legacy)", "directives": "string", "research_bundle": "list[dict]", "system_override": "string | null", "format_validation_error": "string | null", "session_tag": "string | null", "ask": "string | null", "crux_table": "list[dict] | null", "known_unknowns": "list[str] | null"}`. `session_tag` (`quick`, `j1`, `j2`) labels each stateless call. **Judge mode** (FULL stage): `ask` is `"yes"` (J1: P(market resolves YES)) or `"no"` (J2: P(resolves NO), same title and rules); `estimated_p`/`p_low`/`p_high` are the probability of the asked outcome, `## Bull Thesis` always argues YES and `## Bear Thesis` NO. `research_bundle` rows carry `crux_id`/`role` and labels `[E<n> | <age>d | <quality> | unverified]`; `crux_table` lists `{crux_id, role, question, moves_p_if, status, missing, followed_up, evidence_ids}`; `known_unknowns` are unresolved questions. `needs_more_data` is disabled (forced-synthesis override). The Complete object adds optional `base_rate` (0-1 or null, for the asked outcome) and `crux_verdicts: [{crux_id, direction: yes|no|neutral, cited: [E-ids], note}]` (direction is about the market outcome, independent of `ask`). The Hub flips J2 (`1 - p`), takes the mean and the union of the ranges (`aggregate_samples`).
- **Output Schema (state machine):**

```json
{"status": "needs_more_data", "new_queries": ["string"]}
```

`new_queries`: **1-3** non-empty strings (Hub fetches in parallel). Never allowed in `quick` mode or with `system_override`.

```json
{
  "status": "complete",
  "market_id": "string",
  "estimated_p": float,
  "p_low": float,
  "p_high": float,
  "confidence": "low" | "medium" | "high",
  "resolution_checklist": ["string"],
  "key_assumptions": ["string"],
  "as_of": "YYYY-MM-DD",
  "markdown": "string"
}
```

Validated by the Hub: all probabilities in `[0, 1]` and `p_low <= estimated_p <= p_high`. `markdown` is the full wire format (YAML frontmatter with `market_id`, `estimated_p`, optional `p_low`/`p_high`, `error`, then `## Bull Thesis`, `## Bear Thesis`, empty `## Post-Mortem`); the Hub cross-checks `estimated_p` (and `p_low`/`p_high` when present) between the JSON and the frontmatter. The file the Hub writes carries the **aggregated** values of the two FULL judges (J2 flipped, mean `p`, union range) plus Hub-side `q_at_research`, `researched_at`, `end_date`, `samples`, the crux fields and the offer evidence (`evidence_bull`/`evidence_bear`).

**Hub caps (Forensic Pipeline):** FULL stage: one scout, one architect, `RESEARCH_MAX_WEB_CALLS` (12) searches, **one** gap round (`MAX_GAP_ROUNDS = 1`, at most 3 follow-up queries), an audit of at most 10 lines, then two forced-synthesis judges (J1/J2) requiring `status: complete` (no `needs_more_data` rounds, no extra samples); per-query `research_data` capped at **4000** characters; A-IQ poll timeout **`AIQ_TIMEOUT_SEC`**; A-IQ results cached 24h.

Hub persistence: cumulative A-IQ results live in `02_Active_Research/research_bundles/{market_id}.json`; the query caches in `02_Active_Research/aiq_cache.json` and `web_cache.json` (web arm, see `docs/02`). The bundle envelope also stores `evidence_source` / `evidence_fallback`.

## 4. Trade execution (no agent)

The former Trade Executioner agent is removed. Phase 4 is deterministic Hub code: `compute_allocation` -> in-process `execute_polymarket_trade` (`docs/04_skills_contracts.md` section 3) -> trade log + ledger. See `02_orchestrator_pipeline.md` section 4.

## 5. The Post-Mortem Analyst

- **System Prompt:** Audit the decision, not just the outcome: "a 70% call that loses can be *correct*". Classify the cause as one `failure_class` and write exactly one diagnostic paragraph.
- **Input Schema:** `{"market_id", "original_research": "string", "execution_log": "string", "resolution_data": "dict", "p", "p_low", "p_high", "q_entry", "side", "price", "size_usd", "outcome_yes", "realised_pnl_usd", "category", "gate_score", "research_confidence", "resolution_checklist"}`. The structured fields are `null` when unavailable (for example no bet was placed); `outcome_yes` is YES=1/NO=0; `realised_pnl_usd` is after tax (same math as `stats.md`).
- **Output Schema:**

```json
{
  "market_id": "string",
  "post_mortem_analysis": "string",
  "failure_class": "bad_info | bad_reasoning | resolution_misread | variance | correct | new:<slug>",
  "failure_tag_definition": "string | null (required for new:<slug>)",
  "error": "string | null"
}
```

`failure_class` is a fixed class or an open tag `new:<slug>` (slug `[a-z0-9_]{3,40}`, plus a one-line `failure_tag_definition`). The Hub (`orchestrator/failure_tags.py`) keeps a registry `00_System/failure_tags.json` `{slug: {definition, count, first_seen, last_seen}}`; tags with `count >= FAILURE_TAG_MIN_COUNT` are *established*. The post-mortem payload also carries `failure_classes`, `established_failure_tags` and `candidate_failure_tags` (`{new:slug: definition}`) so the analyst reuses tags. An invalid class/slug (or a new tag without definition) is stored as `unclassified`. `failure_class` (fixed, `new:<slug>` or `unclassified`) is written to the post-mortem frontmatter and ledgered on a `post_mortem` stage row.

## 6. The Overseer (Strategy Optimizer)

- **System Prompt:** Interpret the stats, change at most 2 knobs, in steps of at most 15%, cite the stat that justifies each change; may edit the Research Protocol text; may not touch sizing. Doing nothing is valid.
- **Input Schema:** `{"post_mortems": "list[dict]" (<= 15), "current_directives": "string", "stats_md": "string", "failure_class_counts": "dict" (fixed classes + established `new:` tags), "candidate_failure_tags": "dict" (`{new:slug: {count, definition}}`, below threshold, not evidence), "bounds": "list[dict]"}` (`bounds` rows: `knob, current, default, min, max, max_step_frac, last_changed_version, locked`).
- **Output Schema:**

```json
{
  "new_directives_markdown": "string",
  "rationale": "string",
  "error": "string | null"
}
```

The `filters:` yaml block may contain only the knobs in `config.trading_constants.BOUNDS` (see `02_orchestrator_pipeline.md` section 6); the Hub rejects the whole document on any bounds, step, count, cooldown or unknown-key violation, and owns `version` / `knob_last_changed` / `last_overseer_*` in the frontmatter.

## Rollout checklist (breaking changes)

Contract changes from the Wave 3 research redesign (re-check the live prompts against these after any change to `agents_blueprint/<role>/`):

| Role | Critical contract changes |
|------|---------------------------|
| **web_searcher** | Agent for the `web` evidence arm. Input `{market_id, queries: list[str], today}`; tools `web_search`, `web_fetch`. Output `{market_id, results: [{query, research_data (<= 4000 chars, sourced and dated facts), error}], error}`. Price-blind: it must never report prediction-market or betting prices/odds; the Hub also redacts such lines. The Hub calls it with ONE query per call (parallel) so each query gets the full search budget. |
| **web_extractor** | Agent for the Brave backend of the `web` arm (no tools). Input `{market_id, query, today, pages: [{url, title, page_age, text}]}` (pages fetched by the Hub); output same as web_searcher with exactly one result. Facts come only from the given pages, so the citation guard accepts exactly the fetched URLs; `| unknown` dates are filled from Brave's `page_age`. Price-blind like web_searcher. |
| **scout** | New. Price-blind landscape map from one broad evidence pull (`evidence` rows); no estimate. See section 2a. |
| **briefer** | Now the crux architect. Input adds `depth` and `scout`; output `cruxes` (role, question, 1-2 queries, `moves_p_if`) instead of 4 fixed-slot `research_queries` (legacy still accepted). `tools.allow` is empty; the Hub runs the searches. See section 2b. |
| **gap_analyzer** | New. One round of crux status plus up to 3 follow-up queries. See section 2c. |
| **auditor** | New. Checks lines against page text. See section 2d. |
| **deep_researcher** | Price-blind input (no `q`), `mode` (`quick`, `judge`), `session_tag`, `ask`, `crux_table`, `known_unknowns`. Complete output adds `p_low`, `p_high`, `confidence`, `resolution_checklist`, `key_assumptions`, `as_of`. JSON state machine, no agent tools. |
| **executioner** | **Removed.** Phase 4 is deterministic Hub code. |

**Hub behavior:** The orchestrator always populates `research_bundle` before spawning Deep Researcher. A-IQ fetches are never invoked by agents. No agent payload contains the market price.

**Compatibility:** Deep Researcher output must be the JSON state machine (`status` `needs_more_data` or `complete`); raw markdown is rejected, with no fallback. Legacy briefer `summary`-only JSON receives a retryable `format_validation_error` directing the model to emit `research_queries`. Remove this briefer adapter once live model outputs are validated.
