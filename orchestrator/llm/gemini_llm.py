"""Gemini via Google AI Studio (generativelanguage.googleapis.com, generateContent)."""

from __future__ import annotations

import logging
from typing import Any

from orchestrator.llm.base import LLMError, LLMResponse
from orchestrator.llm.http_llm import HttpLLM, HttpStatusError

log = logging.getLogger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiLLM(HttpLLM):
    provider_name = "gemini"
    api_key_env = "GEMINI_API_KEY"

    def _url(self) -> str:
        model = self.model[len("models/"):] if self.model.startswith("models/") else self.model
        return f"{GEMINI_BASE_URL}/models/{model}:generateContent"

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._api_key(), "Content-Type": "application/json"}

    def _body(self, system: str, user: str, json_mode: bool, web: bool) -> dict[str, Any]:
        gen_cfg: dict[str, Any] = {"maxOutputTokens": self.max_tokens}
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
        )
