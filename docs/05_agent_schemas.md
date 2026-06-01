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

## 2. The Query Planner (briefer)

- **System Prompt:** "Plan 1–3 targeted A-IQ research queries from the market title, description, and optional planning context. The Hub runs `execute_aiq_query` in parallel — you do not call tools."
- **Input Schema:** `{"market_id": "string", "market_title": "string", "market_description": "string", "planning_context": "string | null"}`
- **Output Schema:**

```json
{
  "market_id": "string",
  "research_queries": ["string"],
  "error": "string | null"
}
```

## 3. The Deep Researcher

- **System Prompt:** "Synthesize the Hub-provided `research_bundle` into bull/bear theses and a calibrated `estimated_p`. Output JSON with `status: needs_more_data` (new A-IQ queries) or `status: complete` (full markdown document)."
- **Input Schema:** `{"market_id": "string", "market_data": "dict", "directives": "string", "research_bundle": "list[dict]", "system_override": "string | null", "format_validation_error": "string | null"}`
- **Output Schema (state machine):**

```json
{"status": "needs_more_data", "new_queries": ["string"]}
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
