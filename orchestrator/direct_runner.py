"""Direct LLM backend for the agent runner (``direct`` mode): Anthropic or OpenRouter.

Stateless: each call is one request (system prompt = the agent's AGENTS.md, user message = the live
prompt built by :mod:`orchestrator.runner`). The provider is ``DIRECT_PROVIDER``:

- ``anthropic`` (default): Messages API with prompt caching; roles whose spec grants tools
  (web_searcher) get the server-side web search/fetch tools.
- ``openrouter``: POST ``/chat/completions`` over ``requests``; JSON roles ask for
  ``response_format: json_object`` and web_searcher gets OpenRouter's ``web`` plugin.

:func:`complete` is a provider-agnostic tool-less text helper (used by the Discord bot).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

import requests

from orchestrator.config import (
    DIRECT_PROVIDER_OPENROUTER,
    OPENROUTER_API_KEY_ENV,
    direct_max_tokens,
    direct_model,
    direct_provider,
    direct_web_max_uses,
    openclaw_agent_max_attempts,
    openclaw_agent_retry_backoff,
    openclaw_agent_timeout,
    openrouter_base_url,
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


def _provider() -> str:
    try:
        return direct_provider()
    except ValueError as exc:
        raise DirectRunnerError(str(exc)) from exc


def _model_for(role: str) -> str:
    try:
        return direct_model(role)
    except ValueError as exc:
        raise DirectRunnerError(str(exc)) from exc


def _require_provider_key() -> str:
    """Check the chosen provider's SDK/key; return the provider name."""
    provider = _provider()
    if provider == DIRECT_PROVIDER_OPENROUTER:
        if not os.environ.get(OPENROUTER_API_KEY_ENV, "").strip():
            raise DirectRunnerError(f"direct runner (openrouter) needs {OPENROUTER_API_KEY_ENV} to be set")
    else:
        _import_anthropic()
        if not os.environ.get(API_KEY_ENV, "").strip():
            raise DirectRunnerError(f"direct runner mode needs {API_KEY_ENV} to be set")
    return provider


def require_api_key() -> None:
    """Fail fast unless the chosen provider is usable.

    anthropic: SDK installed and ``ANTHROPIC_API_KEY`` set. openrouter: ``OPENROUTER_API_KEY`` set and
    every Hub-spawned role resolves a model (``DIRECT_MODEL_<ROLE>`` or ``DIRECT_MODEL_DEFAULT``).
    The non-Hub concierge role is not required.
    """
    provider = _require_provider_key()
    if provider != DIRECT_PROVIDER_OPENROUTER:
        return
    import agents_blueprint  # noqa: PLC0415 - avoid import cycle at module load

    missing = []
    for role, spec in agents_blueprint.AGENTS.items():
        if not spec.get("hub_spawned", True):
            continue
        try:
            direct_model(role)
        except ValueError:
            missing.append(role)
    if missing:
        raise DirectRunnerError(
            "direct runner (openrouter) has no model for role(s) "
            f"{', '.join(sorted(missing))}: set DIRECT_MODEL_<ROLE> or DIRECT_MODEL_DEFAULT"
        )


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
    """Run one agent call through the chosen provider and return the response text."""
    from orchestrator.runner import _build_live_prompt  # noqa: PLC0415 - avoid import cycle

    provider = _require_provider_key()
    user_text = _build_live_prompt(role, spec, payload)
    system = spec.get("system_prompt") or ""
    web = bool(_tools_for(spec))
    if provider == DIRECT_PROVIDER_OPENROUTER:
        json_mode = spec.get("output_model") is not None  # markdown/state-machine roles are not JSON
        return _call_openrouter(role, system, user_text, json_mode=json_mode, web=web)
    return _call_anthropic(role, system, user_text, tools=_tools_for(spec))


def complete(role: str, system_prompt: str, user_text: str) -> str:
    """Provider-agnostic one-shot text completion: no tools, same retries.

    The model is ``direct_model(role)`` (role env, then ``DIRECT_MODEL_DEFAULT``).
    """
    provider = _require_provider_key()
    if provider == DIRECT_PROVIDER_OPENROUTER:
        return _call_openrouter(role, system_prompt, user_text, json_mode=False, web=False)
    return _call_anthropic(role, system_prompt, user_text, tools=[])


# ---------------------------------------------------------------------------
# Anthropic


def _call_anthropic(role: str, system: str, user_text: str, *, tools: list[dict[str, Any]]) -> str:
    anthropic = _import_anthropic()
    # Our own retry loop below owns attempts/backoff; disable the SDK's hidden retries.
    client = anthropic.Anthropic(max_retries=0)

    kwargs: dict[str, Any] = {
        "model": _model_for(role),
        "max_tokens": direct_max_tokens(),
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user_text}],
    }
    if tools:
        kwargs["tools"] = tools

    messages = kwargs["messages"]
    all_text: list[str] = []
    message: Any = None
    for continuation in range(MAX_PAUSE_CONTINUATIONS + 1):
        message = _with_retry(
            role, "Anthropic", lambda: client.messages.create(**kwargs),
            lambda exc: _retryable(anthropic, exc),
        )
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


def _with_retry(
    role: str, label: str, call: Callable[[], Any], retryable: Callable[[Exception], bool]
) -> Any:
    """Shared retry loop: retry classified-transient errors with exponential backoff."""
    attempts = openclaw_agent_max_attempts()
    backoff = openclaw_agent_retry_backoff()
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - classified below
            if not retryable(exc):
                raise
            if attempt >= attempts:
                raise DirectRunnerError(
                    f"{role}: {label} call failed after {attempts} attempts: {exc}"
                ) from exc
            delay = backoff * (2 ** (attempt - 1))
            log.warning("[DIRECT] %s attempt %d/%d failed (%s); retry in %.1fs",
                        role, attempt, attempts, exc, delay)
            time.sleep(delay)
    raise DirectRunnerError(f"{role}: no attempts made")  # unreachable (attempts >= 1)


# ---------------------------------------------------------------------------
# OpenRouter (https://openrouter.ai/docs: POST /chat/completions, response_format, web plugin)


class _OpenRouterHTTPError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status_code = status


def _openrouter_retryable(exc: Exception) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(exc, _OpenRouterHTTPError) and (status in (408, 429) or status >= 500)


def _openrouter_headers() -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {os.environ.get(OPENROUTER_API_KEY_ENV, '').strip()}",
        "Content-Type": "application/json",
    }
    referer = os.environ.get("OPENROUTER_HTTP_REFERER", "").strip()
    title = os.environ.get("OPENROUTER_APP_TITLE", "").strip()
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-Title"] = title
    return headers


def _openrouter_post(body: dict[str, Any]) -> str:
    """One chat-completions request; returns the message text or raises _OpenRouterHTTPError."""
    resp = requests.post(
        f"{openrouter_base_url()}/chat/completions",
        headers=_openrouter_headers(),
        json=body,
        timeout=openclaw_agent_timeout(),
    )
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status_code >= 400:
        err = data.get("error") if isinstance(data, dict) else None
        detail = (err.get("message") if isinstance(err, dict) else None) or (resp.text or "")[:300]
        raise _OpenRouterHTTPError(resp.status_code, str(detail))
    if not isinstance(data, dict):
        raise _OpenRouterHTTPError(502, "non-JSON response")
    if isinstance(data.get("error"), dict):  # 200 with an upstream error body
        err = data["error"]
        code = err.get("code")
        raise _OpenRouterHTTPError(code if isinstance(code, int) else 502, str(err.get("message")))
    try:
        content = data["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise _OpenRouterHTTPError(502, "response has no choices[0].message") from exc
    return content if isinstance(content, str) else ""


def _call_openrouter(
    role: str, system: str, user_text: str, *, json_mode: bool, web: bool
) -> str:
    body: dict[str, Any] = {
        "model": _model_for(role),
        "max_tokens": direct_max_tokens(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if web:
        body["plugins"] = [{"id": "web", "max_results": direct_web_max_uses()}]

    def post() -> str:
        return _openrouter_post(body)  # reads the current `body` (rebound below on the fallback)

    try:
        try:
            return _with_retry(role, "OpenRouter", post, _openrouter_retryable)
        except _OpenRouterHTTPError as exc:
            if exc.status_code != 400 or "response_format" not in body:
                raise
            # Model/provider rejects json_object: retry once without it; format retries cover the rest.
            log.warning("[DIRECT] %s: response_format rejected (%s); retrying without it", role, exc)
            body = {k: v for k, v in body.items() if k != "response_format"}
            return _with_retry(role, "OpenRouter", post, _openrouter_retryable)
    except _OpenRouterHTTPError as exc:
        raise DirectRunnerError(f"{role}: OpenRouter call failed: {exc}") from exc


__all__ = ["DirectRunnerError", "complete", "require_api_key", "run_direct"]
