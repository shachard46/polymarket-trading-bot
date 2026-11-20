"""Agent execution adapter.

Phase code never calls an LLM provider directly. It calls
:func:`spawn_agent`, which returns the raw text response (or a dict if the
runner already deserialised it). Parsing is the caller's responsibility — see
:mod:`orchestrator.parse`.

The runner has three modes, switchable via the ``PMBOT_MODE``
environment variable:

- ``stub`` (default) — returns a deterministic, schema-valid canned response
  per role so CI can exercise the *success* path without any model call.
- ``stub_error``     — returns an explicit ``{"error": ...}`` payload so CI
  can exercise the inactive-flag failure path.
- ``live``           — calls the role's configured LLM directly through
  :mod:`orchestrator.direct_runner` (provider routing in ``config/models.yaml``).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Protocol

import agents_blueprint
from orchestrator.config import (
    RUNNER_MODE_LIVE,
    RUNNER_MODE_STUB_ERROR,
    runner_mode,
)
from orchestrator.agent_outputs import parse_deep_researcher_json
from orchestrator.parse import (
    AgentOutputParseError,
    coerce_deep_researcher_markdown,
    coerce_deep_researcher_state_machine,
    junk_payload_reason,
    normalize_structured_output,
    _role_keys,
    parse_agent_json_or_yaml,
    preprocess_agent_text,
)
from orchestrator.research import parse_deep_researcher
from orchestrator.runner_stubs import (
    STUB_ESTIMATED_P,
    STUB_RESPONSES,
    _deep_researcher_stub,
    _stub_deep_researcher_markdown,
)
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
        from orchestrator.direct_runner import run_direct  # lazy: provider SDKs load on demand

        result = run_direct(role, spec, payload)
    elif mode == RUNNER_MODE_STUB_ERROR:
        result = _spawn_stub_error(role, spec, payload)
    else:
        result = _spawn_stub(role, spec, payload)

    _validate_response(role, spec, result, payload)
    return result


def spawn_many(role: str, payloads: list[dict[str, Any]]) -> list[Any]:
    """Spawn ``role`` once per payload (batched in live mode when the role is configured for it).

    Returns one entry per payload, in order: the validated raw response, or the exception
    :func:`spawn_agent` would have raised. Items that fail the provider call, parsing or
    validation fall back to the normal one-by-one :func:`spawn_agent` path.
    """
    spec = agents_blueprint.AGENTS[role]
    results: list[Any] = [None] * len(payloads)
    live: list[int] = []
    if runner_mode() == RUNNER_MODE_LIVE and len(payloads) >= 2:
        for i, payload in enumerate(payloads):
            try:
                validate_payload(role, "input", spec.get("input_model"), payload)
            except Exception as exc:  # noqa: BLE001 - same exception spawn_agent raises
                results[i] = exc
            else:
                live.append(i)
        from orchestrator.direct_runner import run_many  # lazy: provider SDKs load on demand

        try:
            raw = run_many(role, spec, [payloads[i] for i in live])
        except Exception as exc:  # noqa: BLE001 - e.g. routing errors: handled per item below
            raw = [exc] * len(live)
        for i, res in zip(live, raw):
            if not isinstance(res, Exception):
                try:
                    _validate_response(role, spec, res, payloads[i])
                    results[i] = res
                    continue
                except Exception as exc:  # noqa: BLE001
                    log.info("[RUNNER] batched %s item failed validation (%s); retrying singly", role, exc)
            results[i] = _spawn_single(role, payloads[i])
        return results
    return [_spawn_single(role, p) for p in payloads]


def _spawn_single(role: str, payload: dict[str, Any]) -> Any:
    try:
        return spawn_agent(role, payload)
    except Exception as exc:  # noqa: BLE001 - returned to the caller, in order
        return exc


def prefetch_runner(runner: AgentRunner, role: str, payloads: list[dict[str, Any]]) -> AgentRunner:
    """Runner that serves the first call per payload of ``role`` from one :func:`spawn_many`.

    Only the default :func:`spawn_agent` runner is batched (a custom/injected runner is returned
    unchanged); any other call (retries with a changed payload, other roles) goes to ``runner``.
    """
    if runner is not spawn_agent or len(payloads) < 2:
        return runner
    pending = list(zip(payloads, spawn_many(role, payloads)))

    def run(r: str, payload: dict[str, Any]) -> Any:
        if r == role:
            for pos, (p, res) in enumerate(pending):
                if p == payload:
                    del pending[pos]
                    if isinstance(res, Exception):
                        raise res
                    return res
        return runner(r, payload)

    return run


def _validate_markdown(role: str, spec: dict[str, Any], result: Any, payload: dict[str, Any]) -> None:
    try:
        markdown = coerce_deep_researcher_markdown(result)
        parse_deep_researcher(markdown)
    except (AgentOutputParseError, ValueError) as exc:
        raise AgentSchemaError(role, "output", str(exc)) from exc


def _validate_state_machine(
    role: str, spec: dict[str, Any], result: Any, payload: dict[str, Any]
) -> None:
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


def _validate_structured(role: str, spec: dict[str, Any], result: Any, payload: dict[str, Any]) -> None:
    output_model = spec.get("output_model")
    if output_model is None:
        return

    if isinstance(result, dict):
        parsed = result
    else:
        try:
            parsed = parse_agent_json_or_yaml(result, expected_keys=_role_keys(role))
        except AgentOutputParseError:
            # Let the phase-level parser surface this as a normal inactive-flag event.
            return

    junk = junk_payload_reason(parsed)
    if junk:
        log.warning("%s output is junk: %s; raw[:300]=%r", role, junk, str(result)[:300])
        raise AgentSchemaError(role, "output", f"junk payload: {junk}")

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
                "%s output schema mismatch; response keys: %s; raw[:300]=%r",
                role,
                sorted(parsed.keys()),
                result[:300] if isinstance(result, str) else repr(result)[:300],
            )
        raise


def _validator_for(spec: dict[str, Any]) -> Callable[[str, dict[str, Any], Any, dict[str, Any]], None]:
    """Per-role validator, chosen by the output kind declared in the agent spec."""
    if spec.get("output_is_markdown"):
        return _validate_markdown
    if spec.get("output_is_state_machine"):
        return _validate_state_machine
    return _validate_structured


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
    _validator_for(spec)(role, spec, result, payload)


def _spawn_stub(role: str, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
    log.info("[STUB] spawn role=%s workspace=%s", role, spec.get("workspace_path"))
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


def _build_live_prompt(
    role: str,
    spec: dict[str, Any],
    payload: dict[str, Any],
) -> str:
    """Serialize the orchestrator payload into the user message for the agent."""
    if spec.get("output_is_markdown"):
        response_hint = (
            "Return only the Markdown document required by your output contract."
        )
    elif spec.get("output_is_state_machine"):
        response_hint = (
            "Return ONLY a raw JSON object with status complete. No markdown fences."
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
    return (
        f"{today_line()}\n\n"
        f"{header}\n\n"
        "Input JSON:\n"
        f"{json.dumps(payload, indent=2, sort_keys=True, default=str)}\n\n"
        f"{response_hint}"
    )


__all__ = [
    "AgentRunner",
    "STUB_RESPONSES",
    "_build_live_prompt",
    "prefetch_runner",
    "spawn_agent",
    "spawn_many",
]
