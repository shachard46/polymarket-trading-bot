"""OpenClaw agent execution adapter.

Phase code never imports the OpenClaw SDK directly. It calls
:func:`spawn_agent`, which returns the raw text response (or a dict if the
SDK already deserialised it). Parsing is the caller's responsibility — see
:mod:`orchestrator.parse`.

The runner has three modes, switchable via the ``OPENCLAW_ORCHESTRATOR_MODE``
environment variable:

- ``stub`` (default) — returns a deterministic, schema-valid canned response
  per role so CI can exercise the *success* path without a live gateway.
- ``stub_error``     — returns an explicit ``{"error": ...}`` payload so CI
  can exercise the inactive-flag failure path.
- ``live``           — calls the local OpenClaw Gateway through the
  ``openclaw agent`` CLI, using each agent's ``openclaw_agent_id`` from
  ``agent.yaml``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Protocol

import agents_blueprint
from orchestrator.config import RUNNER_MODE_LIVE, RUNNER_MODE_STUB_ERROR, runner_mode
from orchestrator.agent_outputs import parse_deep_researcher_json
from orchestrator.parse import (
    AgentOutputParseError,
    coerce_deep_researcher_markdown,
    coerce_deep_researcher_state_machine,
    normalize_structured_output,
    parse_agent_json_or_yaml,
    preprocess_agent_text,
)
from orchestrator.openclaw_cli import extract_agent_text, run_agent
from orchestrator.research import parse_deep_researcher
from orchestrator.live_prompt import build_live_response_hint, today_line
from orchestrator.schema_validation import AgentSchemaError, validate_payload

log = logging.getLogger(__name__)


class AgentRunner(Protocol):
    """Callable contract every phase uses to spawn an agent."""

    def __call__(self, role: str, payload: dict[str, Any]) -> Any: ...


def spawn_agent(role: str, payload: dict[str, Any]) -> Any:
    """Spawn the agent registered under ``role`` and return its raw response.

    Raises :class:`AgentSchemaError` if the Hub-side payload does not match
    the local ``input_schema`` declared in ``agent.yaml`` — the cloud agent
    is never invoked with a malformed input.
    """
    spec = agents_blueprint.AGENTS[role]
    validate_payload(role, "input", spec.get("input_model"), payload)

    mode = runner_mode()
    if mode == RUNNER_MODE_LIVE:
        result = _spawn_live(role, spec, payload)
    elif mode == RUNNER_MODE_STUB_ERROR:
        result = _spawn_stub_error(role, spec, payload)
    else:
        result = _spawn_stub(role, spec, payload)

    _validate_response(role, spec, result, payload)
    return result


def _validate_response(
    role: str,
    spec: dict[str, Any],
    result: Any,
    payload: dict[str, Any],
) -> None:
    """Validate the agent response against the local ``output_schema``.

    Empty responses (the bare stub) and explicit errors propagated through
    the ``error`` field are left for the orchestrator's existing parse path
    to handle — this guard catches *structural* mismatches only.
    """
    if result is None or (isinstance(result, str) and not result.strip()):
        return

    if spec.get("output_is_markdown"):
        try:
            markdown = coerce_deep_researcher_markdown(result)
            parse_deep_researcher(markdown)
        except (AgentOutputParseError, ValueError) as exc:
            raise AgentSchemaError(role, "output", str(exc)) from exc
        return

    if spec.get("output_is_state_machine"):
        try:
            if isinstance(result, dict):
                base: Any = result
            else:
                text = result if isinstance(result, str) else str(result)
                try:
                    base = parse_agent_json_or_yaml(preprocess_agent_text(text))
                except AgentOutputParseError:
                    base = result
            coerced = coerce_deep_researcher_state_machine(base, payload)
            parse_deep_researcher_json(coerced)
        except (AgentOutputParseError, ValueError) as exc:
            raise AgentSchemaError(role, "output", str(exc)) from exc
        return

    output_model = spec.get("output_model")
    if output_model is None:
        return

    if isinstance(result, dict):
        parsed = result
    else:
        try:
            parsed = parse_agent_json_or_yaml(result)
        except AgentOutputParseError:
            # Let the phase-level parser surface this as a normal inactive-flag event.
            return

    # Don't re-validate explicit error responses — the agent is reporting a
    # tool failure, not a schema violation.
    if parsed.get("error"):
        return

    parsed = normalize_structured_output(
        role,
        payload,
        parsed,
        output_schema=spec.get("output_schema"),
    )
    try:
        validate_payload(role, "output", output_model, parsed)
    except AgentSchemaError:
        if isinstance(parsed, dict):
            log.warning(
                "%s output schema mismatch; response keys: %s",
                role,
                sorted(parsed.keys()),
            )
        raise


def _spawn_stub(role: str, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
    oc = spec.get("openclaw") or {}
    log.info(
        "[STUB] spawn role=%s workspace=%s openclaw_agent_id=%r",
        role,
        spec.get("workspace_path"),
        oc.get("openclaw_agent_id"),
    )
    log.debug("[STUB] payload: %s", json.dumps(payload, indent=2, default=str))
    builder = STUB_RESPONSES.get(role)
    if builder is None:
        return ""
    return builder(payload)


def _spawn_stub_error(role: str, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
    """Schema-valid response that propagates an explicit ``error`` field.

    Drives the inactive-flag path end-to-end without forcing every market through a
    parse failure (which would only exercise one error branch).
    """
    log.info("[STUB_ERROR] spawn role=%s", role)
    if spec.get("output_is_markdown"):
        market_id = payload.get("market_id", "stub")
        return (
            "---\n"
            f'market_id: "{market_id}"\n'
            f"estimated_p: {STUB_ESTIMATED_P}\n"
            'error: "stub_error mode"\n'
            "---\n\n"
            "## Bull Thesis\n\n(stub)\n\n## Bear Thesis\n\n(stub)\n\n## Post-Mortem\n"
        )
    if spec.get("output_is_state_machine"):
        market_id = payload.get("market_id", "stub")
        return {
            **_deep_researcher_stub(payload),
            "markdown": _stub_deep_researcher_markdown(market_id, error="stub_error mode"),
        }
    builder = STUB_RESPONSES.get(role)
    if builder is None:
        return {"error": "stub_error mode"}
    base = builder(payload)
    if isinstance(base, dict):
        return {**base, "error": "stub_error mode"}
    return {"error": "stub_error mode"}


# ---------------------------------------------------------------------------
# Schema-valid stub responses (success path) — keyed by agent role.
# ---------------------------------------------------------------------------

StubBuilder = Callable[[dict[str, Any]], Any]


def _triage_stub(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_id": payload.get("market_id", "stub"),
        "researchable": True,
        "category": "stub",
        "reason": "stub: researchable",
        "manipulation_risk": "low",
        "manipulation_reason": "stub",
        "error": None,
    }


# Stub estimate: far enough from a q of ~0.26 to escalate, pass the misread guard and
# clear the shrunk, tax-aware Kelly edge (so the stub pipeline reaches a paper trade),
# yet inside the range for a q of ~0.55 (quick-stage ``no_divergence``).
STUB_ESTIMATED_P = 0.55
STUB_P_LOW = 0.51
STUB_P_HIGH = 0.59


def _stub_deep_researcher_markdown(
    market_id: str,
    *,
    estimated_p: float = STUB_ESTIMATED_P,
    p_low: float | None = None,
    p_high: float | None = None,
    error: str | None = None,
) -> str:
    err_yaml = "null" if error is None else f'"{error}"'
    p_low = max(0.0, estimated_p - 0.08) if p_low is None else p_low
    p_high = min(1.0, estimated_p + 0.08) if p_high is None else p_high
    return (
        "---\n"
        f'market_id: "{market_id}"\n'
        f"estimated_p: {estimated_p}\n"
        f"p_low: {p_low}\n"
        f"p_high: {p_high}\n"
        f"error: {err_yaml}\n"
        "---\n\n"
        "## Bull Thesis\n\n(stub bull thesis)\n\n"
        "## Bear Thesis\n\n(stub bear thesis)\n\n"
        "## Post-Mortem\n"
    )


def _briefer_stub(payload: dict[str, Any]) -> dict[str, Any]:
    title = str(payload.get("title", "")).strip()
    today = payload.get("today", "")
    slots = (
        "resolution criteria and polarity",
        "reference class base rate",
        "current state",
        "strongest disconfirming evidence",
    )
    return {
        "market_id": payload.get("market_id", "stub"),
        "research_queries": [f"stub {slot} for {title} as of {today}" for slot in slots],
        "error": None,
    }


def _web_searcher_stub(payload: dict[str, Any]) -> dict[str, Any]:
    today = payload.get("today", "")
    return {
        "market_id": payload.get("market_id", "stub"),
        "results": [
            {
                "query": q,
                "research_data": f"stub web finding for: {q} (source: example.org, {today})",
                "error": None,
            }
            for q in payload.get("queries") or []
        ],
        "error": None,
    }


def _deep_researcher_stub(payload: dict[str, Any]) -> dict[str, Any]:
    market_id = payload.get("market_id", "stub")
    today = payload.get("today") or "2026-01-01"
    return {
        "status": "complete",
        "market_id": market_id,
        "estimated_p": STUB_ESTIMATED_P,
        "p_low": STUB_P_LOW,
        "p_high": STUB_P_HIGH,
        "confidence": "medium",
        "resolution_checklist": ["stub: YES requires the titled event"],
        "key_assumptions": ["stub assumption"],
        "as_of": today,
        "markdown": _stub_deep_researcher_markdown(
            market_id, p_low=STUB_P_LOW, p_high=STUB_P_HIGH
        ),
    }


def _post_mortem_stub(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_id": payload.get("market_id", "stub"),
        "post_mortem_analysis": "stub post-mortem analysis paragraph.",
        "failure_class": "variance",
        "failure_tag_definition": None,
        "error": None,
    }


def _overseer_stub(payload: dict[str, Any]) -> dict[str, Any]:
    # Re-emit the existing directives so the structural validator still passes.
    current = payload.get("current_directives") or ""
    return {
        "new_directives_markdown": current
        or (
            "---\n"
            "title: stub\n"
            "---\n\n"
            "## Research Protocol\n\nstub.\n\n"
            "## Filter Weightings\n\nstub.\n\n"
            "```yaml\nfilters:\n  gate_min_q: 0.05\n```\n\n"
            "## Risk Constraints\n\nstub.\n\n"
            "## Output Requirements\n\nstub.\n"
        ),
        "rationale": "stub: no changes recommended",
        "error": None,
    }


STUB_RESPONSES: dict[str, StubBuilder] = {
    "triage": _triage_stub,
    "briefer": _briefer_stub,
    "web_searcher": _web_searcher_stub,
    "deep_researcher": _deep_researcher_stub,
    "post_mortem_analyst": _post_mortem_stub,
    "overseer": _overseer_stub,
}


def _spawn_live(role: str, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
    oc = spec.get("openclaw") or {}
    agent_id = oc.get("openclaw_agent_id")
    if not agent_id:
        raise RuntimeError(f"agent '{role}' has no openclaw_agent_id in agent.yaml")

    log.info("[LIVE] spawn role=%s openclaw_agent_id=%s", role, agent_id)
    prompt = _build_live_prompt(role, spec, payload, agent_id=agent_id)
    result = run_agent(
        agent_id,
        prompt,
        session_key=_live_session_key(agent_id, role, payload),
    )
    return extract_agent_text(result)


def _live_session_key(agent_id: str, role: str, payload: dict[str, Any]) -> str:
    """Scope OpenClaw transcript state to one market per agent (and per sample ``session_tag``)."""
    if role == "overseer":
        return f"agent:{agent_id}:orch-overseer-directives"
    raw_market_id = str(payload.get("market_id") or "global")
    safe_market_id = "".join(
        char if char.isalnum() or char in "._-" else "-" for char in raw_market_id
    )
    key = f"agent:{agent_id}:orch-{safe_market_id}"
    tag = payload.get("session_tag")
    if tag:
        # Independent Deep Researcher samples must not share a transcript.
        key += "-" + "".join(c if c.isalnum() or c in "._-" else "-" for c in str(tag))
    return key


def _build_live_prompt(
    role: str,
    spec: dict[str, Any],
    payload: dict[str, Any],
    *,
    agent_id: str | None = None,
) -> str:
    """Serialize the orchestrator payload for the target OpenClaw workspace."""
    if spec.get("output_is_markdown"):
        response_hint = (
            "Return only the Markdown document required by your output contract."
        )
    elif spec.get("output_is_state_machine"):
        response_hint = (
            "Return ONLY a raw JSON object discriminated by status "
            "(needs_more_data or complete). No markdown fences."
        )
        extra = spec.get("live_response_hint")
        if extra:
            response_hint = f"{response_hint}\n\n{str(extra).strip()}"
    else:
        out_schema = spec.get("output_schema") or {}
        response_hint = build_live_response_hint(
            out_schema if isinstance(out_schema, dict) else {}
        )
        extra = spec.get("live_response_hint")
        if extra:
            response_hint = f"{response_hint}\n\n{str(extra).strip()}"
    header = f"Run the {role} agent for this orchestrator payload."
    if agent_id:
        header = f"{header}\nOpenClaw agent id: {agent_id}"
    return (
        f"{today_line()}\n\n"
        f"{header}\n\n"
        f"{response_hint}\n\n"
        "Input JSON:\n"
        f"{json.dumps(payload, indent=2, sort_keys=True, default=str)}"
    )


__all__ = [
    "AgentRunner",
    "STUB_RESPONSES",
    "_build_live_prompt",
    "_live_session_key",
    "spawn_agent",
]
