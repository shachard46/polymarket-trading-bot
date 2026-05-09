"""OpenClaw agent execution adapter.

Phase code never imports the OpenClaw SDK directly. It calls
:func:`spawn_agent`, which returns the raw text response (or a dict if the
SDK already deserialised it). Parsing is the caller's responsibility — see
:mod:`orchestrator.parse`.

The runner has two modes, switchable via the ``OPENCLAW_ORCHESTRATOR_MODE``
environment variable:

- ``stub`` (default) — prints the payload and returns ``""`` so phases hit
  the DLQ path. Lets CI exercise the orchestrator without a live gateway.
- ``live``           — calls the OpenClaw Python SDK using the
  ``openclaw_agent_id`` declared in each agent's ``agent.yaml``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from agents import AGENTS
from orchestrator.config import RUNNER_MODE_LIVE, runner_mode

log = logging.getLogger(__name__)


class AgentRunner(Protocol):
    """Callable contract every phase uses to spawn an agent."""

    def __call__(self, role: str, payload: dict[str, Any]) -> Any: ...


def spawn_agent(role: str, payload: dict[str, Any]) -> Any:
    """Spawn the agent registered under ``role`` and return its raw response."""
    spec = AGENTS[role]
    if runner_mode() == RUNNER_MODE_LIVE:
        return _spawn_live(role, spec, payload)
    return _spawn_stub(role, spec, payload)


def _spawn_stub(role: str, spec: dict[str, Any], payload: dict[str, Any]) -> str:
    oc = spec.get("openclaw") or {}
    log.info(
        "[STUB] spawn role=%s workspace=%s openclaw_agent_id=%r",
        role,
        spec.get("workspace_path"),
        oc.get("openclaw_agent_id"),
    )
    log.debug("[STUB] payload: %s", json.dumps(payload, indent=2, default=str))
    return ""


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


__all__ = ["AgentRunner", "spawn_agent"]
