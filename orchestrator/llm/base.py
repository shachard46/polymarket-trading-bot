"""Provider-agnostic LLM base: one instance = one configured model for one role.

``LLM.complete`` is the template method (shared retry loop, logging, error wrapping); subclasses
implement ``_send`` (one provider call) and ``_is_retryable``.
"""

from __future__ import annotations

import abc
import logging
import os
import time
from dataclasses import dataclass
from typing import ClassVar

from orchestrator.config import (
    DEFAULT_DIRECT_MAX_TOKENS,
    DEFAULT_DIRECT_WEB_MAX_USES,
    llm_max_attempts,
    llm_retry_backoff,
)

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """A provider is unavailable or a call failed (after retries where retryable)."""


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLM(abc.ABC):
    provider_name: ClassVar[str]
    api_key_env: ClassVar[str]

    def __init__(
        self,
        model: str,
        max_tokens: int = DEFAULT_DIRECT_MAX_TOKENS,
        web_max_uses: int = DEFAULT_DIRECT_WEB_MAX_USES,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.web_max_uses = web_max_uses

    @classmethod
    def check_key(cls) -> None:
        """Raise :class:`LLMError` unless this provider's API key env var is set."""
        if not os.environ.get(cls.api_key_env, "").strip():
            raise LLMError(f"provider {cls.provider_name!r} needs {cls.api_key_env} to be set")

    def _api_key(self) -> str:
        return os.environ.get(self.api_key_env, "").strip()

    @abc.abstractmethod
    def _send(self, system: str, user: str, json_mode: bool, web: bool) -> LLMResponse:
        """One provider call (no retries)."""

    @abc.abstractmethod
    def _is_retryable(self, exc: Exception) -> bool:
        """True for transient failures (connection, rate limit, 5xx)."""

    def complete(
        self, system: str, user: str, *, json_mode: bool = False, web: bool = False
    ) -> LLMResponse:
        """Run one completion with retry/backoff on transient errors; failures raise :class:`LLMError`."""
        attempts = llm_max_attempts()
        backoff = llm_retry_backoff()
        label = f"{self.provider_name}:{self.model}"
        for attempt in range(1, attempts + 1):
            try:
                resp = self._send(system, user, json_mode, web)
            except LLMError:
                raise
            except Exception as exc:  # noqa: BLE001 - classified below
                if not self._is_retryable(exc):
                    raise LLMError(f"{label} call failed: {exc}") from exc
                if attempt >= attempts:
                    raise LLMError(f"{label} call failed after {attempts} attempts: {exc}") from exc
                delay = backoff * (2 ** (attempt - 1))
                log.warning("[LLM] %s attempt %d/%d failed (%s); retry in %.1fs",
                            label, attempt, attempts, exc, delay)
                time.sleep(delay)
                continue
            log.info("[LLM] %s tokens in=%s out=%s", label, resp.input_tokens, resp.output_tokens)
            return resp
        raise LLMError(f"{label}: no attempts made")  # unreachable (attempts >= 1)
