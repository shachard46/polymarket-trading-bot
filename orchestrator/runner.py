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
  can exercise the DLQ failure path.
- ``live``           — calls the OpenClaw Python SDK using the
  ``openclaw_agent_id`` declared in each agent's ``agent.yaml``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Protocol

from agents import AGENTS
from orchestrator.config import (
    RUNNER_MODE_LIVE,
    RUNNER_MODE_STUB_ERROR,
    runner_mode,
)
from orchestrator.parse import (
    AgentOutputParseError,
    coerce_deep_researcher_markdown,
    parse_agent_json_or_yaml,
)
from orchestrator.research import parse_deep_researcher
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
    spec = AGENTS[role]
    validate_payload(role, "input", spec.get("input_model"), payload)

    mode = runner_mode()
    if mode == RUNNER_MODE_LIVE:
        result = _spawn_live(role, spec, payload)
    elif mode == RUNNER_MODE_STUB_ERROR:
        result = _spawn_stub_error(role, spec, payload)
    else:
        result = _spawn_stub(role, spec, payload)

    _validate_response(role, spec, result)
    return result


def _validate_response(role: str, spec: dict[str, Any], result: Any) -> None:
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

    output_model = spec.get("output_model")
    if output_model is None:
        return

    if isinstance(result, dict):
        parsed = result
    else:
        try:
            parsed = parse_agent_json_or_yaml(result)
        except AgentOutputParseError:
            # Let the phase-level parser surface this as a normal DLQ event.
            return

    # Don't re-validate explicit error responses — the agent is reporting a
    # tool failure, not a schema violation.
    if parsed.get("error"):
        return

    validate_payload(role, "output", output_model, parsed)


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

    Drives the DLQ path end-to-end without forcing every market through a
    parse failure (which would only exercise one error branch).
    """
    log.info("[STUB_ERROR] spawn role=%s", role)
    if spec.get("output_is_markdown"):
        market_id = payload.get("market_id", "stub")
        return (
            "---\n"
            f'market_id: "{market_id}"\n'
            "estimated_p: 0.5\n"
            'error: "stub_error mode"\n'
            "---\n\n"
            "## Bull Thesis\n\n(stub)\n\n## Bear Thesis\n\n(stub)\n\n## Post-Mortem\n"
        )
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


def _evaluator_stub(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_id": payload.get("market_id", "stub"),
        "passed": False,
        "trigger": None,
        "confidence_multiplier": 1.0,
        "details": "stub: no filter fired",
        "error": None,
    }


def _briefer_stub(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_id": payload.get("market_id", "stub"),
        "summary": f"stub context for {payload.get('market_title', '')}",
        "error": None,
    }


def _deep_researcher_stub(payload: dict[str, Any]) -> str:
    market_id = payload.get("market_id", "stub")
    return (
        "---\n"
        f'market_id: "{market_id}"\n'
        "estimated_p: 0.5\n"
        "error: null\n"
        "---\n\n"
        "## Bull Thesis\n\n(stub bull thesis)\n\n"
        "## Bear Thesis\n\n(stub bear thesis)\n\n"
        "## Post-Mortem\n"
    )


def _executioner_stub(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_id": payload.get("market_id", "stub"),
        "allocation_usd": 0.0,
        "executed": False,
        "transaction_hash": None,
        "error": None,
    }


def _post_mortem_stub(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_id": payload.get("market_id", "stub"),
        "post_mortem_analysis": "stub post-mortem analysis paragraph.",
        "error": None,
    }


def _overseer_stub(payload: dict[str, Any]) -> dict[str, Any]:
    # Re-emit the existing directives so the structural validator still passes.
    current = payload.get("current_directives") or ""
    return {
        "new_directives_markdown": current
        or (
            "---\n"
            'version: "stub"\n'
            "---\n\n"
            "## Research Protocol\n\nstub.\n\n"
            "## Filter Weightings\n\nstub.\n\n"
            "## Risk Constraints\n\nstub.\n\n"
            "## Output Requirements\n\nstub.\n"
        ),
        "rationale": "stub: no changes recommended",
        "error": None,
    }


STUB_RESPONSES: dict[str, StubBuilder] = {
    "evaluator": _evaluator_stub,
    "re_evaluator": _evaluator_stub,
    "briefer": _briefer_stub,
    "deep_researcher": _deep_researcher_stub,
    "executioner": _executioner_stub,
    "post_mortem_analyst": _post_mortem_stub,
    "overseer": _overseer_stub,
}


def _spawn_live(role: str, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
    oc = spec.get("openclaw") or {}
    agent_id = oc.get("openclaw_agent_id")
    if not agent_id:
        raise RuntimeError(f"agent '{role}' has no openclaw_agent_id in agent.yaml")

    # Imported lazily so stub-mode CI does not require the SDK installed.
    from openclaw import OpenClawClient  # type: ignore[import-not-found]

    client = OpenClawClient()
    log.info("[LIVE] spawn role=%s openclaw_agent_id=%s", role, agent_id)
    result = client.get_agent(agent_id).execute(input=payload)
    # SDK returns either a raw string completion or an object exposing one;
    # accept both shapes here so phases can stay agnostic.
    if hasattr(result, "output"):
        return result.output
    return result


__all__ = ["AgentRunner", "STUB_RESPONSES", "spawn_agent"]
