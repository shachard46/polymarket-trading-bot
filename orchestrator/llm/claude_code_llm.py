"""Claude Agent SDK provider: Claude on the user's Claude subscription (no Anthropic API key, no API billing).

Each call runs `claude_agent_sdk.query()` (a Claude Code subprocess) in its own event loop with no project settings, a throwaway cwd
and only the WebSearch/WebFetch tools (web calls only). Auth is `CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`); `ANTHROPIC_API_KEY` is blanked
in the subprocess env so a call can never bill the API. Usage counts against the plan's limits, so concurrency is capped.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import threading
from typing import Any

from config.trading_constants import CLAUDE_CODE_MAX_CONCURRENT
from orchestrator.llm.base import LLM, LLMError, LLMResponse

log = logging.getLogger(__name__)

WEB_TOOLS = ["WebSearch", "WebFetch"]
_URL_RE = re.compile(r"https?://[^\s)\]>\"'<]+")
_semaphore = threading.BoundedSemaphore(CLAUDE_CODE_MAX_CONCURRENT)


class _Transient(Exception):
    """A retryable failure (rate limit / 5xx) reported by the CLI."""


def _import_sdk():
    try:
        import claude_agent_sdk  # noqa: PLC0415 - lazy: only needed when this provider is used
    except ImportError as exc:
        raise LLMError("provider 'claude_code' needs the 'claude-agent-sdk' package: pip install claude-agent-sdk") from exc
    return claude_agent_sdk


def _urls_in(content: Any) -> list[str]:
    if isinstance(content, str):
        return _URL_RE.findall(content)
    out: list[str] = []
    for part in content or []:
        text = part.get("text") if isinstance(part, dict) else None
        out.extend(_URL_RE.findall(text) if isinstance(text, str) else [])
    return out


def _tokens(usage: Any) -> tuple[int | None, int | None]:
    if not isinstance(usage, dict):
        return None, None
    def num(k: str) -> int:
        v = usage.get(k)
        return v if isinstance(v, int) else 0
    tin = num("input_tokens") + num("cache_read_input_tokens") + num("cache_creation_input_tokens")
    return tin, num("output_tokens")


class ClaudeCodeLLM(LLM):
    provider_name = "claude_code"
    api_key_env = "CLAUDE_CODE_OAUTH_TOKEN"
    supports_batch = False

    @classmethod
    def check_key(cls) -> None:
        _import_sdk()
        super().check_key()

    def _options(self, sdk: Any, system: str, web: bool, cwd: str) -> Any:
        tools = list(WEB_TOOLS) if web else []
        kwargs: dict[str, Any] = {
            "system_prompt": system,
            "model": self.model,
            "setting_sources": [],
            "cwd": cwd,
            "tools": tools,
            "allowed_tools": tools,
            "permission_mode": "dontAsk",
            "max_turns": max(4, 2 * self.web_max_uses + 2) if web else 1,
            "env": {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": "", self.api_key_env: self._api_key()},
        }
        if self.effort:
            kwargs["effort"] = self.effort
        return sdk.ClaudeAgentOptions(**kwargs)

    async def _run(self, sdk: Any, system: str, user: str, web: bool, cwd: str) -> LLMResponse:
        result: Any = None
        urls: list[str] = []

        def add(found: list[str]) -> None:
            urls.extend(u for u in found if u not in urls)

        async for msg in sdk.query(prompt=user, options=self._options(sdk, system, web, cwd)):
            if isinstance(msg, sdk.ResultMessage):
                result = msg
            elif isinstance(msg, (sdk.UserMessage, sdk.AssistantMessage)) and isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, sdk.ToolResultBlock):
                        add(_urls_in(block.content))
                    elif isinstance(block, sdk.ToolUseBlock) and isinstance(block.input, dict):
                        add(_urls_in(str(block.input.get("url") or "")))
        label = f"{self.provider_name}:{self.model}"
        if result is None:
            raise LLMError(f"{label} returned no result")
        if result.stop_reason == "refusal":
            raise LLMError(f"{label} refused the request (stop_reason=refusal)")
        if result.is_error:
            status = getattr(result, "api_error_status", None)
            msg_text = f"{label} call failed: {result.result or result.subtype} (status {status})"
            if isinstance(status, int) and (status in (408, 429) or status >= 500):
                raise _Transient(msg_text)
            raise LLMError(msg_text)
        text = result.result or ""
        if not text.strip():
            raise LLMError(f"{label} returned an empty result")
        tin, tout = _tokens(result.usage)
        return LLMResponse(
            text, self.provider_name, self.model, tin, tout,
            citations=urls if web else None, notional_cost_usd=result.total_cost_usd,
        )

    def _send(self, system: str, user: str, json_mode: bool, web: bool) -> LLMResponse:
        sdk = _import_sdk()
        with _semaphore, tempfile.TemporaryDirectory(prefix="pmbot-claude-code-") as cwd:
            return asyncio.run(self._run(sdk, system, user, web, cwd))

    def _is_retryable(self, exc: Exception) -> bool:
        sdk = _import_sdk()
        return isinstance(exc, (_Transient, sdk.CLIConnectionError))
