"""OpenRouter /batches adapter: submit shape, poll mapping, custom_id matching, errors, cancel."""

from __future__ import annotations

import json

import pytest

from orchestrator.llm import openrouter_llm
from orchestrator.llm.base import LLMError, LLMRequest, LLMResponse
from orchestrator.llm.openrouter_llm import OpenRouterLLM


@pytest.fixture
def llm(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://or.test/api/v1")
    return OpenRouterLLM("deepseek/x")


def _ok(text):
    return {"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 3, "completion_tokens": 4}}


def test_submit_body_and_key_order(llm, monkeypatch):
    sent = {}

    def fake_post(url, headers, body):
        sent.update(url=url, body=json.loads(json.dumps(body)))
        return {"id": "batch_1", "status": "validating"}

    monkeypatch.setattr(llm, "_post", fake_post)
    handle = llm._batch_submit([LLMRequest("s", "u0", json_mode=True), LLMRequest("s", "u1")])
    assert handle["id"] == "batch_1"
    assert sent["url"] == "https://or.test/api/v1/batches"
    body = sent["body"]
    assert list(body) == ["endpoint", "model", "requests"]
    assert body["endpoint"] == "/v1/chat/completions" and body["model"] == "deepseek/x"
    assert [r["custom_id"] for r in body["requests"]] == ["req-0", "req-1"]
    assert body["requests"][0]["body"]["response_format"] == {"type": "json_object"}
    assert body["requests"][1]["body"]["messages"][1]["content"] == "u1"


def test_submit_without_id_raises(llm, monkeypatch):
    monkeypatch.setattr(llm, "_post", lambda *a: {"status": "validating"})
    with pytest.raises(LLMError):
        llm._batch_submit([LLMRequest("s", "u")])


@pytest.mark.parametrize("status,want", [
    ("validating", "pending"), ("in_progress", "pending"), ("completed", "done"),
    ("failed", "failed"), ("expired", "failed"), ("cancelled", "failed"),
])
def test_poll_mapping(llm, monkeypatch, status, want):
    monkeypatch.setattr(llm, "_get", lambda url, headers: {"id": "batch_1", "status": status})
    assert llm._batch_poll({"id": "batch_1"}) == want


def test_fetch_matches_custom_id_out_of_order_and_item_errors(llm):
    data = {"status": "completed", "results": [
        {"custom_id": "req-2", "response": {"status_code": 200, "body": _ok("c")}, "error": None},
        {"custom_id": "req-0", "response": {"status_code": 200, "body": _ok("a")}, "error": None},
        {"custom_id": "req-1", "response": None, "error": {"message": "nope"}},
    ]}
    out = llm._batch_fetch({"id": "b", "data": data}, 4)
    assert isinstance(out[0], LLMResponse) and out[0].text == "a" and out[0].input_tokens == 3
    assert isinstance(out[1], LLMError) and "nope" in str(out[1])
    assert out[2].text == "c"
    assert out[3] is None  # missing -> sync fallback


def test_fetch_bad_body_is_item_error(llm):
    data = {"results": [{"custom_id": "req-0", "response": {"status_code": 200, "body": {"x": 1}}, "error": None}]}
    out = llm._batch_fetch({"id": "b", "data": data}, 1)
    assert isinstance(out[0], LLMError)


def test_cancel_is_noop(llm, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no HTTP on cancel")

    monkeypatch.setattr(llm, "_post", boom)
    monkeypatch.setattr(llm, "_get", boom)
    monkeypatch.setattr(llm, "_request", boom)
    llm._batch_cancel({"id": "batch_1"})


def test_poll_transient_errors_stay_pending(llm, monkeypatch):
    from orchestrator.llm.http_llm import HttpStatusError

    for status, pending in ((404, True), (429, True), (503, True), (400, False)):
        def boom(url, headers, s=status):
            raise HttpStatusError(s, "x")

        monkeypatch.setattr(llm, "_get", boom)
        if pending:
            assert llm._batch_poll({"id": "b"}) == "pending"
        else:
            with pytest.raises(HttpStatusError):
                llm._batch_poll({"id": "b"})


def test_missing_nullable_error_defaults_to_none():
    from orchestrator.schema_validation import AgentSchemaError, build_model, validate_payload

    m = build_model("T", {"market_id": "string", "error": "string | null"})
    validate_payload("t", "output", m, {"market_id": "x"})  # no error key: ok
    assert m.model_validate({"market_id": "x"}).error is None
    assert m.model_validate({"market_id": "x", "error": "boom"}).error == "boom"
    with pytest.raises(AgentSchemaError):
        validate_payload("t", "output", m, {"error": None})  # other fields still required
