"""OpenRouter provider: POST /chat/completions (response_format json_object, ``web`` plugin)."""

from __future__ import annotations

import os
import logging
from typing import Any, Sequence

from orchestrator.config import openrouter_base_url
from orchestrator.llm.base import LLMError, LLMRequest, LLMResponse, reasoning_scope
from orchestrator.llm.http_llm import HttpLLM, HttpStatusError


log = logging.getLogger(__name__)

_BATCH_STATES = {"completed": "done", "failed": "failed", "expired": "failed", "cancelled": "failed"}


PROVIDER_IGNORE_ENV = "OPENROUTER_PROVIDER_IGNORE"
DEFAULT_PROVIDER_IGNORE = "Alibaba"  # upstream that blocks some answers with HTTP 403 "inappropriate content"


def provider_ignore() -> list[str]:
    """Provider names OpenRouter must not route to: comma list in ``OPENROUTER_PROVIDER_IGNORE`` (default ``Alibaba``).

    Set the variable to an empty string to ignore none.
    """
    raw = os.environ.get(PROVIDER_IGNORE_ENV)
    raw = DEFAULT_PROVIDER_IGNORE if raw is None else raw
    return [p.strip() for p in raw.split(",") if p.strip()]


def _is_content_block(exc: Exception) -> bool:
    return (
        isinstance(exc, HttpStatusError) and exc.status == 403 and "inappropriate content" in str(exc.body).lower()
    )


class OpenRouterLLM(HttpLLM):
    provider_name = "openrouter"
    api_key_env = "OPENROUTER_API_KEY"
    supports_batch = True

    def _url(self) -> str:
        return f"{openrouter_base_url()}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._api_key()}", "Content-Type": "application/json"}
        referer = os.environ.get("OPENROUTER_HTTP_REFERER", "").strip()
        title = os.environ.get("OPENROUTER_APP_TITLE", "").strip()
        if referer:
            headers["HTTP-Referer"] = referer
        if title:
            headers["X-Title"] = title
        return headers

    def _body(self, system: str, user: str, json_mode: bool, web: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self._reasoning() is False:  # documented: reasoning.enabled=false (effort "none" is rejected by some models)
            body["reasoning"] = {"enabled": False}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if web:
            body["plugins"] = [{"id": "web", "max_results": self.web_max_uses}]
        ignore = provider_ignore()
        if ignore:  # OpenRouter provider routing: `provider.ignore` skips these upstreams for this request
            body["provider"] = {**(body.get("provider") or {}), "ignore": ignore}
        return body

    def _send(self, system: str, user: str, json_mode: bool, web: bool) -> LLMResponse:
        try:
            return super()._send(system, user, json_mode, web)
        except HttpStatusError as exc:
            if not _is_content_block(exc):
                raise
            # an upstream content filter can fire on one routing; one more try may land elsewhere
            log.warning("[LLM] openrouter:%s upstream content block (%s); retrying once", self.model, exc)
            return super()._send(system, user, json_mode, web)

    # ---- /batches (async, discounted; results come back unordered, matched on custom_id) ------

    def _batch_submit(self, requests: Sequence[LLMRequest]) -> Any:
        reqs = []
        for i, r in enumerate(requests):
            with reasoning_scope(r.reasoning):
                body = self._body(r.system, r.user, r.json_mode, r.web)
            body.pop("model", None)  # the batch-level model applies
            reqs.append({"custom_id": f"req-{i}", "body": body})
        # Key order matters: endpoint and model must precede requests or the API answers 400.
        payload = {"endpoint": "/v1/chat/completions", "model": self.model, "requests": reqs}
        data = self._post(f"{openrouter_base_url()}/batches", self._headers(), payload)
        batch_id = data.get("id")
        if not isinstance(batch_id, str) or not batch_id:
            raise LLMError(f"openrouter:{self.model}: batch create returned no id")
        return {"id": batch_id, "data": data}

    def _batch_get(self, handle: Any) -> dict[str, Any]:
        data = self._get(f"{openrouter_base_url()}/batches/{handle['id']}", self._headers())
        handle["data"] = data
        return data

    def _batch_poll(self, handle: Any) -> str:
        try:
            status = str(self._batch_get(handle).get("status") or "").lower()
        except Exception as exc:  # noqa: BLE001
            # 404 right after a 202 is read-after-write lag; transient errors stay pending too
            # (base turns any poll exception into "failed"). PMBOT_LLM_BATCH_MAX_WAIT_SEC bounds the wait.
            if (isinstance(exc, HttpStatusError) and exc.status == 404) or self._is_retryable(exc):
                log.info("[LLM] openrouter:%s batch poll transient (%s); still pending", self.model, exc)
                return "pending"
            raise
        return _BATCH_STATES.get(status, "pending")

    def _batch_fetch(self, handle: Any, count: int) -> list[LLMResponse | LLMError | None]:
        label = f"{self.provider_name}:{self.model}"
        data = handle.get("data") or self._batch_get(handle)
        out: list[LLMResponse | LLMError | None] = [None] * count
        for entry in data.get("results") or []:
            cid = entry.get("custom_id") if isinstance(entry, dict) else None
            if not (isinstance(cid, str) and cid.startswith("req-") and cid[4:].isdigit()):
                continue
            slot = int(cid[4:])
            if not 0 <= slot < count:
                continue
            resp = entry.get("response")
            if entry.get("error"):
                err = entry["error"]
                msg = err.get("message") if isinstance(err, dict) else err
                out[slot] = LLMError(f"{label} batch item failed: {msg}")
            elif isinstance(resp, dict) and isinstance(resp.get("body"), dict):
                try:
                    out[slot] = self._parse(resp["body"])
                except Exception as exc:  # noqa: BLE001 - per-item failure
                    out[slot] = LLMError(f"{label} batch item failed: {exc}")
        return out

    def _batch_cancel(self, handle: Any) -> None:
        # OpenRouter has no cancel endpoint (DELETE only works on finished batches).
        log.warning("[LLM] openrouter:%s batch %s cannot be cancelled; it may still be billed",
                    self.model, handle.get("id"))

    def _fallback_body(self, body: dict[str, Any], err: HttpStatusError) -> dict[str, Any] | None:
        # Model/provider rejects json_object: retry once without it; format retries cover the rest.
        if err.status != 400 or "response_format" not in body:
            return None
        return {k: v for k, v in body.items() if k != "response_format"}

    def _parse(self, data: dict[str, Any]) -> LLMResponse:
        if isinstance(data.get("error"), dict):  # 200 with an upstream error body
            err = data["error"]
            code = err.get("code")
            raise HttpStatusError(code if isinstance(code, int) else 502, str(err.get("message")))
        try:
            message = data["choices"][0]["message"]
            content = message.get("content")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise HttpStatusError(502, "response has no choices[0].message") from exc
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        details = usage.get("completion_tokens_details")
        thinking = details.get("reasoning_tokens") if isinstance(details, dict) else None
        return LLMResponse(
            content if isinstance(content, str) else "",
            self.provider_name, self.model,
            usage.get("prompt_tokens"), usage.get("completion_tokens"),
            citations=_citations(message),
            thinking_tokens=thinking if isinstance(thinking, int) else None,
        )


def _citations(message: dict[str, Any]) -> list[str] | None:
    """URLs of the web plugin's ``url_citation`` annotations (None when the field is absent)."""
    ann = message.get("annotations")
    if not isinstance(ann, list):
        return None
    urls: list[str] = []
    for a in ann:
        if isinstance(a, dict) and a.get("type") == "url_citation":
            url = (a.get("url_citation") or {}).get("url")
            if isinstance(url, str) and url and url not in urls:
                urls.append(url)
    return urls
