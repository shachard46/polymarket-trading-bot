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

## 2. The Surgical Query Planner (briefer)

- **System Prompt:** "**Surgical Query Planner** in the Phase 3 **Forensic Pipeline**. Price-blind: you only see the market text and today's date. Emit exactly 4 fixed-slot verification queries, each containing `as of {today}`. The Hub runs `execute_aiq_query` in parallel (`AIQ_TIMEOUT_SEC` poll timeout, 4k chars per result) - you do not call tools."
- **Input Schema:** `{"market_id": "string", "title": "string", "description": "string", "resolution_source": "string | null", "end_date": "string | null", "category": "string | null", "today": "string"}` (no price).
- **Output Schema:**

```json
{
  "market_id": "string",
  "research_queries": ["string"],
  "error": "string | null"
}
```

On success `research_queries` is a list of **exactly 4** non-empty strings, in slot order: (1) resolution criteria and polarity (what resolves YES; does YES equal the title event?), (2) reference class / base rate, (3) current state as of today, (4) strongest disconfirming evidence. Each must contain `as of {today}` (the Hub appends it if missing). On failure: `research_queries` may be `[]` with a non-null `error`.

## 3. The Forensic Fact Verifier (deep_researcher)

The Deep Researcher is **price-blind**: its payload contains no `q`, `yes_price` or any other market price, and it must not be asked to anchor on or default to one. The Hub compares its output to `q` (see `02_orchestrator_pipeline.md` section 3).

- **System Prompt:** "**Forensic Fact Verifier**. Work resolution rules first (polarity!), then reference class, then verified current state as of `today`, then the strongest disconfirming evidence. Output a probability with an honest range. Inconclusive evidence means a wide range and low confidence - never a neutral default. In `mode: quick` you have no bundle and must answer from the market text and your knowledge; in `mode: full` you may use `needs_more_data` once (1-3 follow-up queries)."
- **Input Schema:** `{"market_id": "string", "title": "string", "description": "string", "resolution_source": "string | null", "end_date": "string | null", "category": "string | null", "today": "string", "mode": "string (quick|full)", "directives": "string", "research_bundle": "list[dict]", "system_override": "string | null", "format_validation_error": "string | null", "session_tag": "string | null"}`. `session_tag` (`quick`, `s1`..`s3`) gives each independent sample its own label; calls are stateless.
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

Validated by the Hub: all probabilities in `[0, 1]` and `p_low <= estimated_p <= p_high`. `markdown` is the full wire format (YAML frontmatter with `market_id`, `estimated_p`, optional `p_low`/`p_high`, `error`, then `## Bull Thesis`, `## Bear Thesis`, empty `## Post-Mortem`); the Hub cross-checks `estimated_p` (and `p_low`/`p_high` when present) between the JSON and the frontmatter. The file the Hub writes carries the **aggregated** values of the 3 FULL samples (medians) plus Hub-side `q_at_research`, `researched_at`, `end_date`, `samples`.

**Hub caps (Forensic Pipeline):** FULL stage: **1 round + 1 follow-up round** (`MAX_RESEARCH_ITERATIONS = 2`), then forced synthesis requiring `status: complete`; 3 samples per market; per-query `research_data` capped at **4000** characters; A-IQ poll timeout **`AIQ_TIMEOUT_SEC`**; A-IQ results cached 24h.

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
| **web_searcher** | Agent for the `web` evidence arm. Input `{market_id, queries: list[str], today}`; tools `web_search`, `web_fetch`. Output `{market_id, results: [{query, research_data (<= 4000 chars, sourced and dated facts), error}], error}`. Price-blind: it must never report prediction-market or betting prices/odds; the Hub also redacts such lines. |
| **briefer** | Input is price-blind text (`title`, `description`, `resolution_source`, `end_date`, `category`, `today`). Output `research_queries`: exactly 4 fixed-slot strings with `as of {today}`. `tools.allow` is empty; the Hub runs `execute_aiq_query`. |
| **deep_researcher** | Price-blind input (no `q`), `mode`, `session_tag`. Complete output adds `p_low`, `p_high`, `confidence`, `resolution_checklist`, `key_assumptions`, `as_of`. JSON state machine, no agent tools. |
| **executioner** | **Removed.** Phase 4 is deterministic Hub code. |

**Hub behavior:** The orchestrator always populates `research_bundle` before spawning Deep Researcher. A-IQ fetches are never invoked by agents. No agent payload contains the market price.

**Compatibility:** Deep Researcher output must be the JSON state machine (`status` `needs_more_data` or `complete`); raw markdown is rejected, with no fallback. Legacy briefer `summary`-only JSON receives a retryable `format_validation_error` directing the model to emit `research_queries`. Remove this briefer adapter once live model outputs are validated.
