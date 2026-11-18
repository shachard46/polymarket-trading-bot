"""Shared HTTP plumbing for providers that speak JSON over ``requests``."""

from __future__ import annotations

import abc
import logging
from typing import Any

import requests

from orchestrator.config import llm_timeout
from orchestrator.llm.base import LLM, LLMResponse

log = logging.getLogger(__name__)


class HttpStatusError(Exception):
    """A provider answered with an error status (or an upstream error inside a 200 body)."""

    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.status_code = status
        self.body = body


class HttpLLM(LLM, abc.ABC):
    """Template: ``_send`` = ``_body`` -> POST ``_url`` -> ``_parse``; subclasses supply the hooks."""

    @abc.abstractmethod
    def _url(self) -> str: ...

    @abc.abstractmethod
    def _headers(self) -> dict[str, str]: ...

    @abc.abstractmethod
    def _body(self, system: str, user: str, json_mode: bool, web: bool) -> dict[str, Any]: ...

    @abc.abstractmethod
    def _parse(self, data: dict[str, Any]) -> LLMResponse: ...

    def _fallback_body(self, body: dict[str, Any], err: HttpStatusError) -> dict[str, Any] | None:
        """Optionally return a modified body to retry once after ``err``; None means raise."""
        return None

    def _post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", url, headers, body)

    def _get(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        return self._request("GET", url, headers, None)

    def _request(
        self, method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None
    ) -> dict[str, Any]:
        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=llm_timeout())
        else:
            resp = requests.post(url, headers=headers, json=body, timeout=llm_timeout())
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code >= 400:
            err = data.get("error") if isinstance(data, dict) else None
            detail = (err.get("message") if isinstance(err, dict) else None) or (resp.text or "")[:300]
            raise HttpStatusError(resp.status_code, str(detail))
        if not isinstance(data, dict):
            raise HttpStatusError(502, "non-JSON response")
        return data

    def _send(self, system: str, user: str, json_mode: bool, web: bool) -> LLMResponse:
        body = self._body(system, user, json_mode, web)
        url, headers = self._url(), self._headers()
        try:
            data = self._post(url, headers, body)
        except HttpStatusError as exc:
            fallback = self._fallback_body(body, exc)
            if fallback is None:
                raise
            log.warning("[LLM] %s:%s retrying once with a modified body (%s)",
                        self.provider_name, self.model, exc)
            data = self._post(url, headers, fallback)
        return self._parse(data)

    def _is_retryable(self, exc: Exception) -> bool:
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return True
        return isinstance(exc, HttpStatusError) and (exc.status in (408, 429) or exc.status >= 500)
