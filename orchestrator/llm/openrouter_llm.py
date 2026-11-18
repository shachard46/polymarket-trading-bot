"""OpenRouter provider: POST /chat/completions (response_format json_object, ``web`` plugin)."""

from __future__ import annotations

import os
from typing import Any

from orchestrator.config import openrouter_base_url
from orchestrator.llm.base import LLMResponse
from orchestrator.llm.http_llm import HttpLLM, HttpStatusError


class OpenRouterLLM(HttpLLM):
    provider_name = "openrouter"
    api_key_env = "OPENROUTER_API_KEY"

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
        if self.reasoning is False:  # documented: reasoning.enabled=false (effort "none" is rejected by some models)
            body["reasoning"] = {"enabled": False}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if web:
            body["plugins"] = [{"id": "web", "max_results": self.web_max_uses}]
        return body

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
        return LLMResponse(
            content if isinstance(content, str) else "",
            self.provider_name, self.model,
            usage.get("prompt_tokens"), usage.get("completion_tokens"),
            citations=_citations(message),
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
