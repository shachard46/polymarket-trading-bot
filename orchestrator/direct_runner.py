"""Direct Anthropic Messages API backend for the agent runner (``direct`` mode).

Stateless: each call is one Messages request (system prompt = the agent's AGENTS.md with
prompt caching, user message = the live prompt built by :mod:`orchestrator.runner`). Only
roles whose spec grants tools (web_searcher) get the server-side web search/fetch tools.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from orchestrator.config import (
    direct_max_tokens,
    direct_model,
    direct_web_max_uses,
    openclaw_agent_max_attempts,
    openclaw_agent_retry_backoff,
)

log = logging.getLogger(__name__)

API_KEY_ENV = "ANTHROPIC_API_KEY"
WEB_SEARCH_TOOL_TYPE = "web_search_20260209"
WEB_FETCH_TOOL_TYPE = "web_fetch_20260209"
MAX_PAUSE_CONTINUATIONS = 3


class DirectRunnerError(RuntimeError):
    """Direct backend is unavailable or a call failed after all retries."""


def _import_anthropic():
    try:
        import anthropic  # noqa: PLC0415 - lazy: only needed in direct mode
    except ImportError as exc:
        raise DirectRunnerError(
            "direct runner mode needs the 'anthropic' package: pip install anthropic"
        ) from exc
    return anthropic


def require_api_key() -> None:
    """Raise a clear error unless the SDK is installed and ANTHROPIC_API_KEY is set."""
    _import_anthropic()
    if not os.environ.get(API_KEY_ENV, "").strip():
        raise DirectRunnerError(f"direct runner mode needs {API_KEY_ENV} to be set")


def _tools_for(spec: dict[str, Any]) -> list[dict[str, Any]]:
    tools = (spec.get("openclaw") or {}).get("tools") or {}
    allow = tools.get("allow") if isinstance(tools, dict) else None
    if not allow:
        return []
    max_uses = direct_web_max_uses()
    out: list[dict[str, Any]] = []
    if "web_search" in allow:
        out.append({"type": WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": max_uses})
    if "web_fetch" in allow:
        out.append({"type": WEB_FETCH_TOOL_TYPE, "name": "web_fetch", "max_uses": max_uses})
    return out


def _text_of(message: Any) -> str:
    parts = [
        b.text for b in (getattr(message, "content", None) or [])
        if getattr(b, "type", None) == "text" and getattr(b, "text", None)
    ]
    return "".join(parts)


def _retryable(anthropic: Any, exc: Exception) -> bool:
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.RateLimitError)):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(exc, anthropic.APIStatusError) and isinstance(status, int) and status >= 500


def run_direct(role: str, spec: dict[str, Any], payload: dict[str, Any]) -> str:
    """Run one agent call through the Anthropic API and return the concatenated text."""
    from orchestrator.runner import _build_live_prompt  # noqa: PLC0415 - avoid import cycle

    anthropic = _import_anthropic()
    require_api_key()
    # Our own retry loop below owns attempts/backoff; disable the SDK's hidden retries.
    client = anthropic.Anthropic(max_retries=0)

    model = direct_model(role)
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": direct_max_tokens(),
        "system": [
            {
                "type": "text",
                "text": spec.get("system_prompt") or "",
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": _build_live_prompt(role, spec, payload)}],
    }
    tools = _tools_for(spec)
    if tools:
        kwargs["tools"] = tools

    messages = kwargs["messages"]
    all_text: list[str] = []
    message: Any = None
    for continuation in range(MAX_PAUSE_CONTINUATIONS + 1):
        message = _create_with_retry(anthropic, client, role, kwargs)
        all_text.append(_text_of(message))
        if getattr(message, "stop_reason", None) != "pause_turn":
            break
        if continuation >= MAX_PAUSE_CONTINUATIONS:
            log.warning("[DIRECT] %s still paused after %d continuations", role, continuation)
            break
        # Server-tool turn paused: replay the assistant content and let it continue.
        messages = [*messages, {"role": "assistant", "content": message.content}]
        kwargs["messages"] = messages
    # Only the final message's text: narrative between searches would break JSON parsing.
    return _text_of(message) or "".join(all_text)


def _create_with_retry(anthropic: Any, client: Any, role: str, kwargs: dict[str, Any]) -> Any:
    attempts = openclaw_agent_max_attempts()
    backoff = openclaw_agent_retry_backoff()
    for attempt in range(1, attempts + 1):
        try:
            return client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - classified below
            if not _retryable(anthropic, exc):
                raise
            if attempt >= attempts:
                raise DirectRunnerError(
                    f"{role}: Anthropic call failed after {attempts} attempts: {exc}"
                ) from exc
            delay = backoff * (2 ** (attempt - 1))
            log.warning("[DIRECT] %s attempt %d/%d failed (%s); retry in %.1fs",
                        role, attempt, attempts, exc, delay)
            time.sleep(delay)
    raise DirectRunnerError(f"{role}: no attempts made")  # unreachable (attempts >= 1)


__all__ = ["DirectRunnerError", "require_api_key", "run_direct"]
