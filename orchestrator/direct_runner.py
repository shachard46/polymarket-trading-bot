"""Live-mode agent backend: thin seam over :mod:`orchestrator.llm` (provider + model per role).

Stateless: each call is one request (system prompt = the agent's AGENTS.md, user message = the live
prompt built by :mod:`orchestrator.runner`). :func:`complete` is the tool-less text helper used by the
Discord bot.
"""

from __future__ import annotations

from typing import Any

from orchestrator.llm import routing
from orchestrator.llm.base import LLMError

DirectRunnerError = LLMError

_WEB_TOOLS = frozenset({"web_search", "web_fetch"})


def require_api_key() -> None:
    """Fail fast unless every Hub-spawned role resolves and its provider key is set."""
    routing.validate()


def _web_for(spec: dict[str, Any]) -> bool:
    tools = spec.get("tools") or {}
    allow = tools.get("allow") if isinstance(tools, dict) else None
    return bool(allow and _WEB_TOOLS & set(allow))


def run_direct(role: str, spec: dict[str, Any], payload: dict[str, Any]) -> str:
    """Run one agent call through the role's configured provider and return the response text."""
    from orchestrator.runner import _build_live_prompt  # noqa: PLC0415 - avoid import cycle

    user_text = _build_live_prompt(role, spec, payload)
    json_mode = spec.get("output_model") is not None  # markdown/state-machine roles are not JSON
    return routing.llm_for(role).complete(
        spec.get("system_prompt") or "", user_text, json_mode=json_mode, web=_web_for(spec)
    ).text


def complete(role: str, system_prompt: str, user_text: str) -> str:
    """One-shot text completion for ``role``: no tools, no JSON mode, same retries."""
    return routing.llm_for(role).complete(system_prompt, user_text).text


__all__ = ["DirectRunnerError", "complete", "require_api_key", "run_direct"]
