"""Provider-agnostic LLM base: one instance = one configured model for one role.

``LLM.complete`` is the template method (shared retry loop, logging, error wrapping); subclasses
implement ``_send`` (one provider call) and ``_is_retryable``.
"""

from __future__ import annotations

import abc
import contextlib
import contextvars
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, ClassVar, Iterator, Sequence

from orchestrator.config import (
    DEFAULT_DIRECT_MAX_TOKENS,
    DEFAULT_DIRECT_WEB_MAX_USES,
    llm_batch_max_wait_sec,
    llm_batch_poll_sec,
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
    citations: list[str] | None = None  # URLs the provider's web tool retrieved (None = unknown)
    thinking_tokens: int | None = None  # thinking part of output_tokens (already included there); None = unknown


@dataclass
class LLMRequest:
    """One single-shot call for :meth:`LLM.complete_many`."""

    system: str
    user: str
    json_mode: bool = False
    web: bool = False
    market_id: str | None = None  # usage attribution (usage.market_scope)
    reasoning: bool | None = None  # per-call override of the role's reasoning (False = lowest thinking)


# Per-call reasoning override (set by complete() and batch submit; wins over the role's reasoning).
_reasoning_override: contextvars.ContextVar[bool | None] = contextvars.ContextVar("llm_reasoning", default=None)


@contextlib.contextmanager
def reasoning_scope(reasoning: bool | None) -> Iterator[None]:
    token = _reasoning_override.set(reasoning)
    try:
        yield
    finally:
        _reasoning_override.reset(token)


def _usage_record(role: str | None, resp: LLMResponse, batch: bool) -> None:
    try:
        from orchestrator.llm import usage  # noqa: PLC0415 - usage imports this module

        usage.record(role or "", resp, batch=batch)
    except Exception:  # noqa: BLE001 - accounting must never break a call
        log.warning("llm usage record failed", exc_info=True)


def _market_scope(market_id: str | None):
    from orchestrator.llm import usage  # noqa: PLC0415

    return usage.market_scope(market_id)


class LLM(abc.ABC):
    provider_name: ClassVar[str]
    api_key_env: ClassVar[str]
    supports_batch: ClassVar[bool] = False  # provider has an async batch API (see _batch_* hooks)

    def __init__(
        self,
        model: str,
        max_tokens: int = DEFAULT_DIRECT_MAX_TOKENS,
        web_max_uses: int = DEFAULT_DIRECT_WEB_MAX_USES,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.web_max_uses = web_max_uses
        self.temperature: float | None = None  # per-role (models.yaml); None = provider default
        self.reasoning: bool | None = None  # False = disable thinking; None = provider default
        self.role: str | None = None  # set by routing.llm_for (usage attribution)
        self.batch_enabled = True  # routing sets this from the role's `batch` flag in models.yaml

    def _reasoning(self) -> bool | None:
        """Effective reasoning flag: the per-call override if set, else the role's."""
        override = _reasoning_override.get()
        return self.reasoning if override is None else override

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
        self, system: str, user: str, *, json_mode: bool = False, web: bool = False,
        reasoning: bool | None = None,
    ) -> LLMResponse:
        """Run one completion with retry/backoff on transient errors; failures raise :class:`LLMError`.

        reasoning=False lowers thinking for this call only (providers without the knob ignore it).
        """
        attempts = llm_max_attempts()
        backoff = llm_retry_backoff()
        label = f"{self.provider_name}:{self.model}"
        for attempt in range(1, attempts + 1):
            try:
                with reasoning_scope(reasoning):
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
            _usage_record(self.role, resp, False)
            return resp
        raise LLMError(f"{label}: no attempts made")  # unreachable (attempts >= 1)

    # ---- batch API (latency-insensitive single-shot calls) -------------------------------------
    # Providers with `supports_batch` implement the four hooks; handles are opaque to the base.

    def _batch_submit(self, requests: Sequence[LLMRequest]) -> Any:
        raise NotImplementedError

    def _batch_poll(self, handle: Any) -> str:
        """``"pending"``, ``"done"`` (results ready) or ``"failed"`` (batch will never complete)."""
        raise NotImplementedError

    def _batch_fetch(self, handle: Any, count: int) -> list[LLMResponse | LLMError | None]:
        """Per-request results in input order; ``None`` = no result (canceled/expired/missing)."""
        raise NotImplementedError

    def _batch_cancel(self, handle: Any) -> None:
        raise NotImplementedError

    def _complete_one(self, req: LLMRequest) -> LLMResponse | LLMError:
        try:
            with _market_scope(req.market_id):
                return self.complete(req.system, req.user, json_mode=req.json_mode, web=req.web,
                                     reasoning=req.reasoning)
        except LLMError as exc:
            return exc

    def complete_many(self, items: Sequence[LLMRequest]) -> list[LLMResponse | LLMError]:
        """Run independent calls, batched when the provider supports it, else sequentially.

        Results are in input order; a failed item is an :class:`LLMError` entry (never raised). Batch
        failure or timeout (``PMBOT_LLM_BATCH_MAX_WAIT_SEC``) cancels the batch and runs the unfinished
        items through the synchronous :meth:`complete`, so a tick never stalls beyond the max wait.
        """
        items = list(items)
        results: list[LLMResponse | LLMError | None] = [None] * len(items)
        # Web (server-tool, pause_turn) calls stay synchronous.
        idx = [i for i, it in enumerate(items) if not it.web]
        if self.supports_batch and self.batch_enabled and len(idx) >= 2:
            self._run_batch(items, idx, results)
        for i, it in enumerate(items):
            if results[i] is None:
                results[i] = self._complete_one(it)
        return results  # type: ignore[return-value]

    def _run_batch(
        self, items: list[LLMRequest], idx: list[int], results: list[LLMResponse | LLMError | None]
    ) -> None:
        label = f"{self.provider_name}:{self.model}"
        try:
            handle = self._batch_submit([items[i] for i in idx])
        except Exception as exc:  # noqa: BLE001 - fall back to sync
            log.warning("[LLM] %s batch submit failed (%s); running synchronously", label, exc)
            return
        poll, max_wait = llm_batch_poll_sec(), llm_batch_max_wait_sec()
        deadline = time.monotonic() + max_wait
        state = "pending"
        while True:
            try:
                state = self._batch_poll(handle)
            except Exception as exc:  # noqa: BLE001
                log.warning("[LLM] %s batch poll failed (%s)", label, exc)
                state = "failed"
            if state != "pending" or time.monotonic() >= deadline:
                break
            time.sleep(min(poll, max(0.0, deadline - time.monotonic())))
        if state != "done":
            log.warning("[LLM] %s batch %s; cancelling, falling back to sync",
                        label, "failed" if state == "failed" else f"timed out after {max_wait}s")
            try:
                self._batch_cancel(handle)
            except Exception as exc:  # noqa: BLE001
                log.warning("[LLM] %s batch cancel failed: %s", label, exc)
            return
        try:
            fetched = self._batch_fetch(handle, len(idx))
        except Exception as exc:  # noqa: BLE001
            log.warning("[LLM] %s batch fetch failed (%s); running synchronously", label, exc)
            return
        for pos, i in enumerate(idx):
            res = fetched[pos] if pos < len(fetched) else None
            if isinstance(res, LLMResponse):
                log.info("[LLM] %s batch tokens in=%s out=%s", label, res.input_tokens, res.output_tokens)
                with _market_scope(items[i].market_id):
                    _usage_record(self.role, res, True)
            results[i] = res  # LLMError kept as the item result; None falls back to sync
