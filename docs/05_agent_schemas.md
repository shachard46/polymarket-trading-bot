# Agent Personas and I/O Schemas

This document defines exactly _how_ agents behave and their strict JSON/YAML boundaries. All JSON outputs must handle explicit failure states.

Each role's [`agents_blueprint/<role>/agent.yaml`](../agents_blueprint/) declares `input_schema` and `output_schema`. In live mode the orchestrator builds the response hint from `output_schema` automatically. Optional `live_response_hint` (multiline string) appends extra rules when the schema alone is insufficient (e.g. Overseer markdown-in-JSON structure).

## 1. The Evaluator & Re-Evaluator

- **System Prompt:** Evaluator: first-time quantitative gate; calls `evaluate_market_metrics` and reasons over the returned signal_bundle. Re-Evaluator: same skill contract when Active Research already exists; receives `historic_signal_bundle` from the prior filter log for regime comparison.
- **Input Schema — Evaluator:** `{"market_id": "string", "filter_directives": "dict"}`
- **Input Schema — Re-Evaluator:** `{"market_id": "string", "review_kind": "string", "filter_directives": "dict", "historic_signal_bundle": "dict | null", "prior_filter_trigger": "string | null", "prior_evaluator_details": "string | null", "prior_filter_log": "dict | null", "research_markdown": "string | null", "trade_log": "dict | null"}`
- **Output Schema (Re-Evaluator):**

```json
  {
    "market_id": "string",
    "passed": boolean,
    "trigger": "string | null",
    "confidence_multiplier": float,
    "details": "string",
    "signal_bundle": "dict | null",
    "error": "string | null",
    "retry_deep_research": boolean,
    "refresh_reason": "string | null"
  }


```

- **Output Schema (Evaluator):**

```json
  {
    "market_id": "string",
    "passed": boolean,
    "trigger": "string | null",
    "confidence_multiplier": float,
    "details": "string",
    "signal_bundle": "dict | null",
    "error": "string | null"
  }


```

## 2. The Surgical Query Planner (briefer)

- **System Prompt:** "**Surgical Query Planner** in the Phase 3 **Forensic Pipeline** (high velocity + high rigor). Engineer 1–3 unevadable verification questions per turn targeting vulnerabilities, legal conditions, and structural anomalies in the market copy. Forbidden: open-ended thematic queries. The Hub runs `execute_aiq_query` in parallel (300s batch timeout, 4k chars per result) — you do not call tools."
- **Input Schema:** `{"market_id": "string", "market_title": "string", "market_description": "string", "planning_context": "string | null"}`
- **Output Schema:**

```json
{
  "market_id": "string",
  "research_queries": ["string"],
  "error": "string | null"
}
```

On success: `research_queries` must contain **1–3** non-empty strings and `error` is `null`. On failure: `research_queries` may be `[]` with a non-null `error`.

## 3. The Forensic Fact Verifier (deep_researcher)

- **System Prompt:** "**Forensic Fact Verifier** in the Phase 3 **Forensic Pipeline**. Verify `research_bundle` with prosecutorial rigor; use `needs_more_data` as exhaustive interrogation when any pricing-critical fact is not 100% verified (1–3 surgical follow-up queries). Output `status: complete` only with ironclad conviction in `estimated_p`. Bull/Bear sections: exactly 2–3 maximum-density, fact-backed asymmetric bullets each (not narrative summaries)."
- **Input Schema:** `{"market_id": "string", "market_data": "dict", "directives": "string", "research_bundle": "list[dict]", "system_override": "string | null", "format_validation_error": "string | null"}`
- **Output Schema (state machine):**

```json
{"status": "needs_more_data", "new_queries": ["string"]}
```

`new_queries`: **1–3** non-empty strings (Hub fetches in parallel).
```

```json
{
  "status": "complete",
  "market_id": "string",
  "estimated_p": float,
  "markdown": "string"
}
```

`markdown` is the full `02_Active_Research/{market_id}.md` wire format (YAML frontmatter + `## Bull Thesis`, `## Bear Thesis`, empty `## Post-Mortem`).

**Hub caps (Forensic Pipeline):** Up to **4** Deep Researcher iterations (`MAX_RESEARCH_ITERATIONS`); per-query `research_data` capped at **4000** characters; Phase 3 A-IQ batch poll timeout **300s** (`AIQ_BATCH_POLL_TIMEOUT`, batch-only). After the iteration cap, forced synthesis requires `status: complete`.

Hub persistence: cumulative A-IQ results live in `02_Active_Research/research_bundles/{market_id}.json`.

## 4. The Trade Executioner

- **System Prompt:** "You are a deterministic executor. Map `market_data` to `(q, D, L, V)` per the prompt, call `calculate_trade_allocation`, then call `execute_polymarket_trade` only when `paper_trade_mode` is false and allocation > 0."
- **Input Schema:** `{"market_id": "string", "p_value": float, "market_data": "dict", "paper_trade_mode": boolean}` (`paper_trade_mode` mirrors `PAPER_TRADE_MODE` from the Hub).
- **Output Schema:**

```json
{
  "market_id": "string",
  "allocation_usd": float,
  "score": "float | null",
  "below_edge_threshold": "boolean | null",
  "executed": boolean,
  "transaction_hash": "string | null",
  "error": "string | null"
}

```

## 5. The Post-Mortem Analyst

- **System Prompt:** "You are a retrospective analyst. You will be given a resolved market's original research report, the trade execution logs, and the final market resolution data. Explain what data points led the Deep Researcher to the correct or incorrect conclusion. Output exactly one paragraph."
- **Input Schema:** `{"market_id": "string", "original_research": "string", "execution_log": "string", "resolution_data": "dict"}`
- **Output Schema:**

```json
{
  "market_id": "string",
  "post_mortem_analysis": "string",
  "error": "string | null"
}
```

## 6. The Overseer (Strategy Optimizer)

- **System Prompt:** "You are the macro-learner. Analyze the provided batch of Post-Mortem reports and Trade Logs. Identify which quantitative filters are producing false-positive alpha. Output a completely rewritten Markdown string for `active_directives.md` adjusting the rules, risk tolerances, and focus areas for the Deep Researcher."
- **Input Schema:** `{"post_mortems": "list[dict]", "current_directives": "string"}`
- **Output Schema:**

```json
{
  "new_directives_markdown": "string",
  "rationale": "string",
  "error": "string | null"
}
```

## Phase 3 rollout checklist (breaking changes)

After the Hub-managed qualitative pipeline lands, sync each OpenClaw workspace from [`agents_blueprint/<role>/`](../agents_blueprint/) before running live orchestrator ticks:

| Role | OpenClaw agent id | Critical contract changes |
|------|-------------------|---------------------------|
| **briefer** | `polymarket-briefer` | Output `research_queries` (1–3 strings), not `summary`. `tools.allow` is empty — Hub runs `execute_aiq_query`. |
| **deep_researcher** | `polymarket-deep-researcher` | Input `research_bundle` (list), not `context_summary`. Output JSON state machine (`status: needs_more_data` \| `complete`), not raw markdown. No agent tools. |

**Hub behavior:** The orchestrator always populates `research_bundle` before spawning Deep Researcher. A-IQ fetches are never invoked from agent workspaces.

**Temporary compatibility:** Legacy Deep Researcher raw-markdown responses are coerced to `status: complete` in [`orchestrator/parse.py`](../orchestrator/parse.py) (`coerce_deep_researcher_state_machine`) so rollout does not immediately flag markets inactive. Legacy briefer `summary`-only JSON receives a retryable `format_validation_error` directing the model to emit `research_queries`. Remove reliance on these adapters once live workspaces are validated.
