"""Live-mode agent backend: thin seam over :mod:`orchestrator.llm` (provider + model per role).

Stateless: each call is one request (system prompt = the agent's AGENTS.md, user message = the live
prompt built by :mod:`orchestrator.runner`). :func:`complete` is the tool-less text helper used by the
Discord bot.
"""

from __future__ import annotations

import contextvars
from typing import Any

from orchestrator.llm import routing
from orchestrator.llm import usage
from orchestrator.llm.base import LLMError, LLMRequest, LLMResponse

DirectRunnerError = LLMError

_WEB_TOOLS = frozenset({"web_search", "web_fetch"})


# Retrieved URLs (and model id) of the most recent direct call; each run_direct overwrites it, so after
# format retries the accepted (last) call's citations are the ones that count.
_last_call: contextvars.ContextVar[tuple[list[str] | None, str | None]] = contextvars.ContextVar(
    "direct_last_call", default=(None, None)
)


def last_citations() -> list[str] | None:
    """URLs the last :func:`run_direct` call's web tool retrieved (None = unknown / no such call)."""
    return _last_call.get()[0]


def last_model() -> str | None:
    return _last_call.get()[1]


def reset_last_call() -> None:
    _last_call.set((None, None))


def require_api_key() -> None:
    """Fail fast unless every Hub-spawned role resolves and its provider key is set."""
    routing.validate()


def _web_for(spec: dict[str, Any]) -> bool:
    tools = spec.get("tools") or {}
    allow = tools.get("allow") if isinstance(tools, dict) else None
    return bool(allow and _WEB_TOOLS & set(allow))


def run_direct(role: str, spec: dict[str, Any], payload: dict[str, Any]) -> str:
    """Run one agent call through the role's configured provider and return the response text."""
    reset_last_call()
    with usage.market_scope(payload.get("market_id")):
        resp = routing.llm_for(role).complete(
            spec.get("system_prompt") or "", _live_prompt(role, spec, payload),
            json_mode=_json_mode(spec), web=_web_for(spec),
        )
    _last_call.set((getattr(resp, "citations", None), getattr(resp, "model", None)))
    return resp.text


def _json_mode(spec: dict[str, Any]) -> bool:
    return spec.get("output_model") is not None  # markdown/state-machine roles are not JSON


def _live_prompt(role: str, spec: dict[str, Any], payload: dict[str, Any]) -> str:
    from orchestrator.runner import _build_live_prompt  # noqa: PLC0415 - avoid import cycle

    return _build_live_prompt(role, spec, payload)


def run_many(role: str, spec: dict[str, Any], payloads: list[dict[str, Any]]) -> list[str | Exception]:
    """Run several independent calls of one role (batched when the role's provider/config allow it).

    Results are in input order; a failed item is an exception instance (not raised).
    """
    llm = routing.llm_for(role)
    reqs = [
        LLMRequest(
            spec.get("system_prompt") or "", _live_prompt(role, spec, p),
            json_mode=_json_mode(spec), web=_web_for(spec), market_id=p.get("market_id"),
        )
        for p in payloads
    ]
    out: list[str | Exception] = []
    for res in llm.complete_many(reqs):
        out.append(res.text if isinstance(res, LLMResponse) else res)
    return out


def complete(role: str, system_prompt: str, user_text: str) -> str:
    """One-shot text completion for ``role``: no tools, no JSON mode, same retries."""
    return routing.llm_for(role).complete(system_prompt, user_text).text


__all__ = ["DirectRunnerError", "complete", "last_citations", "last_model", "reset_last_call", "require_api_key", "run_direct", "run_many"]
