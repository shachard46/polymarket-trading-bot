"""Anthropic Messages API provider (prompt caching, server-side web tools, pause_turn continuation)."""

from __future__ import annotations

import logging
import os
from typing import Any

from orchestrator.llm.base import LLM, LLMError, LLMResponse

log = logging.getLogger(__name__)

WEB_SEARCH_TOOL_TYPE = "web_search_20260209"
WEB_FETCH_TOOL_TYPE = "web_fetch_20260209"
MAX_PAUSE_CONTINUATIONS = 3


def _import_anthropic():
    try:
        import anthropic  # noqa: PLC0415 - lazy: only needed when this provider is used
    except ImportError as exc:
        raise LLMError("provider 'anthropic' needs the 'anthropic' package: pip install anthropic") from exc
    return anthropic


def _text_of(message: Any) -> str:
    parts = [
        b.text for b in (getattr(message, "content", None) or [])
        if getattr(b, "type", None) == "text" and getattr(b, "text", None)
    ]
    return "".join(parts)


def _tokens(message: Any) -> tuple[int, int]:
    usage = getattr(message, "usage", None)
    i, o = getattr(usage, "input_tokens", 0), getattr(usage, "output_tokens", 0)
    return (i if isinstance(i, int) else 0, o if isinstance(o, int) else 0)


class AnthropicLLM(LLM):
    provider_name = "anthropic"
    api_key_env = "ANTHROPIC_API_KEY"

    @classmethod
    def check_key(cls) -> None:
        _import_anthropic()
        super().check_key()

    def _tools(self) -> list[dict[str, Any]]:
        return [
            {"type": WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": self.web_max_uses},
            {"type": WEB_FETCH_TOOL_TYPE, "name": "web_fetch", "max_uses": self.web_max_uses},
        ]

    def _send(self, system: str, user: str, json_mode: bool, web: bool) -> LLMResponse:
        anthropic = _import_anthropic()
        # The base class owns attempts/backoff; disable the SDK's hidden retries.
        client = anthropic.Anthropic(max_retries=0)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
        }
        if web:
            kwargs["tools"] = self._tools()
        messages = kwargs["messages"]
        all_text: list[str] = []
        tin = tout = 0
        message: Any = None
        for continuation in range(MAX_PAUSE_CONTINUATIONS + 1):
            message = client.messages.create(**kwargs)
            i, o = _tokens(message)
            tin, tout = tin + i, tout + o
            all_text.append(_text_of(message))
            if getattr(message, "stop_reason", None) != "pause_turn":
                break
            if continuation >= MAX_PAUSE_CONTINUATIONS:
                log.warning("[LLM] anthropic:%s still paused after %d continuations", self.model, continuation)
                break
            # Server-tool turn paused: replay the assistant content and let it continue.
            messages = [*messages, {"role": "assistant", "content": message.content}]
            kwargs["messages"] = messages
        # Only the final message's text: narrative between searches would break JSON parsing.
        text = _text_of(message) or "".join(all_text)
        return LLMResponse(text, self.provider_name, self.model, tin, tout)

    def _is_retryable(self, exc: Exception) -> bool:
        anthropic = _import_anthropic()
        if isinstance(exc, (anthropic.APIConnectionError, anthropic.RateLimitError)):
            return True
        status = getattr(exc, "status_code", None)
        return isinstance(exc, anthropic.APIStatusError) and isinstance(status, int) and status >= 500
