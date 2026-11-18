"""Gemini via Google AI Studio (generativelanguage.googleapis.com, generateContent)."""

from __future__ import annotations

import logging
from typing import Any, Sequence

from orchestrator.llm.base import LLMError, LLMRequest, LLMResponse
from orchestrator.llm.http_llm import HttpLLM, HttpStatusError

log = logging.getLogger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiLLM(HttpLLM):
    provider_name = "gemini"
    api_key_env = "GEMINI_API_KEY"
    supports_batch = True

    def _url(self) -> str:
        model = self.model[len("models/"):] if self.model.startswith("models/") else self.model
        return f"{GEMINI_BASE_URL}/models/{model}:generateContent"

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._api_key(), "Content-Type": "application/json"}

    def _body(self, system: str, user: str, json_mode: bool, web: bool) -> dict[str, Any]:
        gen_cfg: dict[str, Any] = {"maxOutputTokens": self.max_tokens}
        if self.temperature is not None:
            gen_cfg["temperature"] = self.temperature
        if self.reasoning is False:
            # Lowest thinking level the model supports ("minimal" is not supported on Gemini 4 models).
            level = "low" if "gemini-4" in self.model else "minimal"
            gen_cfg["thinkingConfig"] = {"thinkingLevel": level}
        if json_mode:
            gen_cfg["responseMimeType"] = "application/json"
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": gen_cfg,
        }
        if web:
            body["tools"] = [{"googleSearch": {}}]
        return body

    # ---- batchGenerateContent (inline requests; async, discounted) ----------------------------

    def _model_name(self) -> str:
        return self.model[len("models/"):] if self.model.startswith("models/") else self.model

    def _batch_submit(self, requests: Sequence[LLMRequest]) -> Any:
        reqs = [
            {
                "request": self._body(r.system, r.user, r.json_mode, r.web),
                "metadata": {"key": f"k{i}"},
            }
            for i, r in enumerate(requests)
        ]
        body = {
            "batch": {
                "display_name": f"pmbot-{self.role or 'llm'}-{len(reqs)}",
                "input_config": {"requests": {"requests": reqs}},
            }
        }
        url = f"{GEMINI_BASE_URL}/models/{self._model_name()}:batchGenerateContent"
        data = self._post(url, self._headers(), body)
        name = data.get("name") or (data.get("batch") or {}).get("name")
        if not isinstance(name, str) or not name:
            raise LLMError(f"gemini:{self.model}: batch create returned no name")
        return {"name": name.lstrip("/"), "data": data}

    @staticmethod
    def _batch_state(data: dict[str, Any]) -> str:
        state = data.get("state") or (data.get("metadata") or {}).get("state") or ""
        return str(state).upper()

    def _batch_get(self, handle: Any) -> dict[str, Any]:
        data = self._get(f"{GEMINI_BASE_URL}/{handle['name']}", self._headers())
        handle["data"] = data
        return data

    def _batch_poll(self, handle: Any) -> str:
        data = self._batch_get(handle)
        state = self._batch_state(data)
        if state.endswith("SUCCEEDED"):
            return "done"
        if state.endswith(("FAILED", "CANCELLED", "EXPIRED")):
            return "failed"
        return "pending"

    @staticmethod
    def _inlined(data: dict[str, Any]) -> list[Any]:
        """Inline responses from either documented shape (`dest.inlinedResponses` / `response.inlinedResponses`)."""
        for holder in (data.get("dest"), data.get("response"), (data.get("metadata") or {}).get("output")):
            if not isinstance(holder, dict):
                continue
            inl = holder.get("inlinedResponses")
            if isinstance(inl, dict):  # {"inlinedResponses": [...]} nesting
                inl = inl.get("inlinedResponses")
            if isinstance(inl, list):
                return inl
        return []

    def _batch_fetch(self, handle: Any, count: int) -> list[LLMResponse | LLMError | None]:
        label = f"{self.provider_name}:{self.model}"
        data = handle.get("data") or self._batch_get(handle)
        entries = self._inlined(data)
        out: list[LLMResponse | LLMError | None] = [None] * count
        for pos, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            key = (entry.get("metadata") or {}).get("key")
            slot = int(key[1:]) if isinstance(key, str) and key[1:].isdigit() else pos
            if not 0 <= slot < count:
                continue
            if entry.get("error"):
                err = entry["error"]
                msg = err.get("message") if isinstance(err, dict) else err
                out[slot] = LLMError(f"{label} batch item failed: {msg}")
            elif isinstance(entry.get("response"), dict):
                try:
                    out[slot] = self._parse(entry["response"])
                except LLMError as exc:
                    out[slot] = exc
        return out

    def _batch_cancel(self, handle: Any) -> None:
        self._post(f"{GEMINI_BASE_URL}/{handle['name']}:cancel", self._headers(), {})

    def _fallback_body(self, body: dict[str, Any], err: HttpStatusError) -> dict[str, Any] | None:
        """JSON mode may be rejected together with a tool: retry once without JSON mode (400 only)."""
        gen_cfg = body.get("generationConfig", {})
        if err.status != 400 or "tools" not in body or "responseMimeType" not in gen_cfg:
            return None
        new_cfg = {k: v for k, v in gen_cfg.items() if k != "responseMimeType"}
        return {**body, "generationConfig": new_cfg}

    def _parse(self, data: dict[str, Any]) -> LLMResponse:
        label = f"{self.provider_name}:{self.model}"
        candidates = data.get("candidates") or []
        if not candidates:
            block = (data.get("promptFeedback") or {}).get("blockReason")
            reason = f"prompt blocked ({block})" if block else "no candidates in response"
            raise LLMError(f"{label}: {reason}")
        cand = candidates[0]
        finish = cand.get("finishReason")
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(
            p["text"] for p in parts
            if isinstance(p, dict) and isinstance(p.get("text"), str) and not p.get("thought")
        )
        if not text.strip():
            raise LLMError(f"{label}: empty response (finishReason={finish or 'unknown'})")
        if finish not in (None, "STOP"):
            log.warning("[LLM] %s finishReason=%s", label, finish)
        usage = data.get("usageMetadata") or {}
        inp = usage.get("promptTokenCount")
        if inp is not None:
            inp += usage.get("toolUsePromptTokenCount") or 0  # search results fed to the model, billed as input
        out = usage.get("candidatesTokenCount")
        if out is not None:
            out += usage.get("thoughtsTokenCount") or 0  # thinking tokens are billed as output
        return LLMResponse(
            text=text,
            provider=self.provider_name,
            model=self.model,
            input_tokens=inp,
            output_tokens=out,
            citations=_grounding_urls(cand) if "groundingMetadata" in cand else None,
        )


def _grounding_urls(cand: dict[str, Any]) -> list[str]:
    chunks = (cand.get("groundingMetadata") or {}).get("groundingChunks") or []
    urls: list[str] = []
    for ch in chunks:
        uri = (ch.get("web") or {}).get("uri") if isinstance(ch, dict) else None
        if isinstance(uri, str) and uri and uri not in urls:
            urls.append(uri)
    return urls
