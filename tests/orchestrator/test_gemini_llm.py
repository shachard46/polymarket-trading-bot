"""GeminiLLM: request body, parsing, blocked responses, retry classification, JSON+tool fallback."""

from __future__ import annotations

import pytest
import requests

from orchestrator.llm import base as llm_base
from orchestrator.llm import http_llm
from orchestrator.llm.base import LLMError
from orchestrator.llm.gemini_llm import GeminiLLM


class FakeResp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code = status
        self._data = data
        self.text = text or str(data)

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


def ok(text="hi", **extra):
    data = {
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 7, "totalTokenCount": 18},
    }
    data.update(extra)
    return FakeResp(200, data)


@pytest.fixture
def llm(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k-test")
    monkeypatch.setenv("PMBOT_LLM_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("PMBOT_LLM_RETRY_BACKOFF", "0")
    monkeypatch.setattr(llm_base.time, "sleep", lambda s: None)
    return GeminiLLM(model="gemini-x", max_tokens=123)


def fake_post(monkeypatch, responses):
    calls = []
    queue = list(responses)

    def _post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(http_llm.requests, "post", _post)
    return calls


def test_class_attrs():
    assert GeminiLLM.provider_name == "gemini"
    assert GeminiLLM.api_key_env == "GEMINI_API_KEY"


def test_url_and_headers(llm):
    assert llm._url() == "https://generativelanguage.googleapis.com/v1beta/models/gemini-x:generateContent"
    assert GeminiLLM(model="models/gemini-y")._url().endswith("/models/gemini-y:generateContent")
    assert llm._headers()["x-goog-api-key"] == "k-test"


def test_body_plain(llm):
    body = llm._body("SYS", "USER", False, False)
    assert body == {
        "systemInstruction": {"parts": [{"text": "SYS"}]},
        "contents": [{"role": "user", "parts": [{"text": "USER"}]}],
        "generationConfig": {"maxOutputTokens": 123},
    }


def test_body_json_mode(llm):
    body = llm._body("S", "U", True, False)
    assert body["generationConfig"] == {"maxOutputTokens": 123, "responseMimeType": "application/json"}
    assert "tools" not in body


def test_body_web(llm):
    body = llm._body("S", "U", False, True)
    assert body["tools"] == [{"googleSearch": {}}]
    assert "responseMimeType" not in body["generationConfig"]


def test_complete_parses_text_and_usage(llm, monkeypatch):
    calls = fake_post(monkeypatch, [ok("a")])
    resp = llm.complete("S", "U", json_mode=True)
    assert (resp.text, resp.provider, resp.model) == ("a", "gemini", "gemini-x")
    assert (resp.input_tokens, resp.output_tokens) == (11, 7)
    assert calls[0]["headers"]["x-goog-api-key"] == "k-test"
    assert calls[0]["json"]["generationConfig"]["responseMimeType"] == "application/json"


def test_parse_joins_parts_skips_thoughts_counts_thinking(llm):
    data = {
        "candidates": [{"content": {"parts": [
            {"text": "plan", "thought": True}, {"text": "foo"}, {"text": "bar"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2, "thoughtsTokenCount": 5},
    }
    resp = llm._parse(data)
    assert resp.text == "foobar"
    assert resp.output_tokens == 7


def test_parse_tool_use_prompt_tokens_count_as_input(llm):
    data = {
        "candidates": [{"content": {"parts": [{"text": "x"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 10, "toolUsePromptTokenCount": 30, "candidatesTokenCount": 2},
    }
    assert llm._parse(data).input_tokens == 40


def test_parse_missing_usage(llm):
    resp = llm._parse({"candidates": [{"content": {"parts": [{"text": "x"}]}}]})
    assert resp.input_tokens is None and resp.output_tokens is None


def test_blocked_prompt_raises_non_retryable(llm, monkeypatch):
    calls = fake_post(monkeypatch, [FakeResp(200, {"promptFeedback": {"blockReason": "SAFETY"}})])
    with pytest.raises(LLMError, match="blocked.*SAFETY"):
        llm.complete("S", "U")
    assert len(calls) == 1


def test_no_candidates_raises(llm):
    with pytest.raises(LLMError, match="no candidates"):
        llm._parse({})


def test_empty_candidate_raises_with_finish_reason(llm, monkeypatch):
    calls = fake_post(monkeypatch, [FakeResp(200, {"candidates": [{"finishReason": "SAFETY"}]})])
    with pytest.raises(LLMError, match="SAFETY"):
        llm.complete("S", "U")
    assert len(calls) == 1


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_retryable_statuses_retry_then_succeed(llm, monkeypatch, status):
    calls = fake_post(monkeypatch, [FakeResp(status, {"error": {"message": "boom"}}), ok("done")])
    assert llm.complete("S", "U").text == "done"
    assert len(calls) == 2


def test_connection_error_retries(llm, monkeypatch):
    calls = fake_post(monkeypatch, [requests.ConnectionError("down"), ok("done")])
    assert llm.complete("S", "U").text == "done"
    assert len(calls) == 2


def test_retries_exhausted(llm, monkeypatch):
    calls = fake_post(monkeypatch, [FakeResp(503, {"error": {"message": "x"}})] * 3)
    with pytest.raises(LLMError, match="after 3 attempts"):
        llm.complete("S", "U")
    assert len(calls) == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_non_retryable_statuses(llm, monkeypatch, status):
    calls = fake_post(monkeypatch, [FakeResp(status, {"error": {"message": "nope"}})])
    with pytest.raises(LLMError, match="nope"):
        llm.complete("S", "U")
    assert len(calls) == 1


def test_json_plus_tool_400_falls_back_without_json_mode(llm, monkeypatch):
    calls = fake_post(monkeypatch, [
        FakeResp(400, {"error": {"message": "JSON mode not supported with tools"}}), ok("{}")])
    resp = llm.complete("S", "U", json_mode=True, web=True)
    assert resp.text == "{}"
    assert len(calls) == 2
    assert calls[0]["json"]["generationConfig"]["responseMimeType"] == "application/json"
    assert "responseMimeType" not in calls[1]["json"]["generationConfig"]
    assert calls[1]["json"]["tools"] == [{"googleSearch": {}}]
    assert calls[1]["json"]["generationConfig"]["maxOutputTokens"] == 123


def test_no_fallback_without_tool_or_for_other_status(llm, monkeypatch):
    # 400 with json_mode but no tool: no fallback
    calls = fake_post(monkeypatch, [FakeResp(400, {"error": {"message": "bad"}})])
    with pytest.raises(LLMError):
        llm.complete("S", "U", json_mode=True)
    assert len(calls) == 1
    # 403 with json+tool: no fallback
    calls = fake_post(monkeypatch, [FakeResp(403, {"error": {"message": "bad"}})])
    with pytest.raises(LLMError):
        llm.complete("S", "U", json_mode=True, web=True)
    assert len(calls) == 1


def test_check_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiLLM.check_key()
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    GeminiLLM.check_key()
