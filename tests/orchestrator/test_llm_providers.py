"""Provider classes (Anthropic SDK + OpenRouter HTTP) and the base retry template: mocked, no network."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from orchestrator.llm import anthropic_llm, base, http_llm
from orchestrator.llm.anthropic_llm import AnthropicLLM
from orchestrator.llm.base import LLM, LLMError, LLMResponse
from orchestrator.llm.http_llm import HttpLLM, HttpStatusError
from orchestrator.llm.openrouter_llm import OpenRouterLLM


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setenv("PMBOT_LLM_RETRY_BACKOFF", "0")
    monkeypatch.setattr(base.time, "sleep", lambda s: None)


# ---------------------------------------------------------------- base template


class _Fake(LLM):
    provider_name, api_key_env = "fake", "FAKE_KEY"

    def __init__(self, script):
        super().__init__("m")
        self.script, self.calls = script, []

    def _send(self, system, user, json_mode, web):
        self.calls.append((system, user, json_mode, web))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def _is_retryable(self, exc):
        return isinstance(exc, TimeoutError)


def test_complete_drives_send_and_retries(monkeypatch):
    monkeypatch.setenv("PMBOT_LLM_MAX_ATTEMPTS", "3")
    ok = LLMResponse("hi", "fake", "m", 1, 2)
    llm = _Fake([TimeoutError("a"), TimeoutError("b"), ok])
    assert llm.complete("s", "u", json_mode=True, web=True) is ok
    assert llm.calls == [("s", "u", True, True)] * 3


def test_complete_exhausted_and_non_retryable(monkeypatch):
    monkeypatch.setenv("PMBOT_LLM_MAX_ATTEMPTS", "2")
    llm = _Fake([TimeoutError("a"), TimeoutError("b")])
    with pytest.raises(LLMError, match="after 2 attempts"):
        llm.complete("s", "u")
    llm = _Fake([ValueError("bad"), LLMResponse("x", "fake", "m")])
    with pytest.raises(LLMError, match="bad"):
        llm.complete("s", "u")
    assert len(llm.calls) == 1


def test_check_key(monkeypatch):
    monkeypatch.delenv("FAKE_KEY", raising=False)
    with pytest.raises(LLMError, match="FAKE_KEY"):
        _Fake.check_key()
    monkeypatch.setenv("FAKE_KEY", "k")
    _Fake.check_key()


# ---------------------------------------------------------------- Anthropic


class _APIConnectionError(Exception):
    pass


class _RateLimitError(Exception):
    pass


class _APIStatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class _Block:
    def __init__(self, type_, text=""):
        self.type, self.text = type_, text


class _Msg:
    def __init__(self, blocks, stop_reason="end_turn", usage=None):
        self.content, self.stop_reason = blocks, stop_reason
        if usage:
            self.usage = types.SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1])


class _Client:
    instances: list["_Client"] = []
    script: list[Any] = []

    def __init__(self, **kwargs):
        self.init_kwargs, self.calls, self.messages = kwargs, [], self
        _Client.instances.append(self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = _Client.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture()
def sdk(monkeypatch):
    mod = types.ModuleType("anthropic")
    mod.Anthropic, mod.APIConnectionError = _Client, _APIConnectionError
    mod.RateLimitError, mod.APIStatusError = _RateLimitError, _APIStatusError
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    _Client.instances, _Client.script = [], []
    return _Client


def _ok(text="{}", usage=(3, 4)):
    return _Msg([_Block("server_tool_use"), _Block("text", text), _Block("text", "!")], usage=usage)


def test_anthropic_payload_cache_text_usage(sdk):
    sdk.script = [_ok('{"a":1}')]
    r = AnthropicLLM("claude-x", max_tokens=99).complete("SYS", "USER", json_mode=True)
    assert (r.text, r.provider, r.model, r.input_tokens, r.output_tokens) == ('{"a":1}!', "anthropic", "claude-x", 3, 4)
    kw = sdk.instances[0].calls[0]
    assert kw["system"] == [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]
    assert kw["messages"] == [{"role": "user", "content": "USER"}]
    assert kw["model"] == "claude-x" and kw["max_tokens"] == 99 and "tools" not in kw
    assert sdk.instances[0].init_kwargs == {"max_retries": 0}


def test_anthropic_web_tools(sdk):
    sdk.script = [_ok()]
    AnthropicLLM("m", web_max_uses=5).complete("s", "u", web=True)
    tools = sdk.instances[0].calls[0]["tools"]
    assert {t["name"] for t in tools} == {"web_search", "web_fetch"}
    assert {t["type"] for t in tools} == {anthropic_llm.WEB_SEARCH_TOOL_TYPE, anthropic_llm.WEB_FETCH_TOOL_TYPE}
    assert all(t["max_uses"] == 5 for t in tools)


def test_anthropic_pause_turn_final_text_and_cap(sdk):
    paused = _Msg([_Block("text", "searching")], stop_reason="pause_turn", usage=(1, 1))
    sdk.script = [paused, _Msg([_Block("text", "final")], usage=(2, 2))]
    r = AnthropicLLM("m").complete("s", "u", web=True)
    assert r.text == "final" and (r.input_tokens, r.output_tokens) == (3, 3)
    assert sdk.instances[0].calls[1]["messages"][-1] == {"role": "assistant", "content": paused.content}
    sdk.instances.clear()
    sdk.script = [_Msg([_Block("text", f"t{i}")], stop_reason="pause_turn") for i in range(5)]
    assert AnthropicLLM("m").complete("s", "u", web=True).text == "t3"
    assert len(sdk.instances[0].calls) == 4


def test_anthropic_empty_final_falls_back_to_all_text(sdk):
    sdk.script = [_Msg([_Block("text", "partial")], stop_reason="pause_turn"), _Msg([_Block("server_tool_use")])]
    assert AnthropicLLM("m").complete("s", "u").text == "partial"


def test_anthropic_retry_classification(sdk, monkeypatch):
    monkeypatch.setenv("PMBOT_LLM_MAX_ATTEMPTS", "4")
    sdk.script = [_APIConnectionError("x"), _RateLimitError("r"), _APIStatusError(503), _ok("fine")]
    assert AnthropicLLM("m").complete("s", "u").text == "fine!"
    sdk.script = [_APIStatusError(400), _ok()]
    sdk.instances.clear()
    with pytest.raises(LLMError, match="status 400"):
        AnthropicLLM("m").complete("s", "u")
    assert sum(len(c.calls) for c in sdk.instances) == 1


def test_anthropic_check_key(sdk, monkeypatch):
    AnthropicLLM.check_key()
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        AnthropicLLM.check_key()
    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises(LLMError, match="pip install anthropic"):
        AnthropicLLM.check_key()


# ---------------------------------------------------------------- HTTP / OpenRouter


class _Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._data, self.text = status, data, text

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


def _or_ok(text="{}", usage=None):
    d: dict[str, Any] = {"choices": [{"message": {"role": "assistant", "content": text}}]}
    if usage:
        d["usage"] = usage
    return _Resp(200, d)


def _or_err(status, msg="nope"):
    return _Resp(status, {"error": {"code": status, "message": msg}})


@pytest.fixture()
def http(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    calls, script = [], []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(http_llm.requests, "post", fake_post)
    return types.SimpleNamespace(calls=calls, script=script)


def test_openrouter_payload_and_usage(http):
    http.script[:] = [_or_ok('{"a":1}', {"prompt_tokens": 5, "completion_tokens": 6})]
    r = OpenRouterLLM("vendor/m", max_tokens=7).complete("SYS", "USER", json_mode=True)
    assert (r.text, r.provider, r.model, r.input_tokens, r.output_tokens) == ('{"a":1}', "openrouter", "vendor/m", 5, 6)
    c = http.calls[0]
    assert c["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert c["headers"]["Authorization"] == "Bearer or-test"
    assert "HTTP-Referer" not in c["headers"] and "X-Title" not in c["headers"]
    assert c["json"] == {
        "model": "vendor/m", "max_tokens": 7,
        "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}],
        "response_format": {"type": "json_object"},
        "provider": {"ignore": ["Alibaba"]},
    }


def test_openrouter_optional_headers_base_url_and_web(http, monkeypatch):
    monkeypatch.setenv("OPENROUTER_HTTP_REFERER", "https://example.test")
    monkeypatch.setenv("OPENROUTER_APP_TITLE", "bot")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "http://proxy/v1/")
    http.script[:] = [_or_ok()]
    OpenRouterLLM("m", web_max_uses=3).complete("s", "u", web=True)
    c = http.calls[0]
    assert c["url"] == "http://proxy/v1/chat/completions"
    assert c["headers"]["HTTP-Referer"] == "https://example.test" and c["headers"]["X-Title"] == "bot"
    assert c["json"]["plugins"] == [{"id": "web", "max_results": 3}]
    assert "response_format" not in c["json"]


def test_openrouter_json_400_falls_back_once(http):
    http.script[:] = [_or_err(400, "response_format unsupported"), _or_ok("fine")]
    assert OpenRouterLLM("m").complete("s", "u", json_mode=True).text == "fine"
    assert "response_format" in http.calls[0]["json"] and "response_format" not in http.calls[1]["json"]


def test_openrouter_400_without_response_format_raises(http):
    http.script[:] = [_or_err(400)]
    with pytest.raises(LLMError, match="HTTP 400"):
        OpenRouterLLM("m").complete("s", "u")
    assert len(http.calls) == 1


def test_http_retry_classification(http, monkeypatch):
    monkeypatch.setenv("PMBOT_LLM_MAX_ATTEMPTS", "4")
    http.script[:] = [http_llm.requests.ConnectionError("x"), _or_err(429), _or_err(503), _or_ok("ok")]
    assert OpenRouterLLM("m").complete("s", "u").text == "ok"
    assert len(http.calls) == 4
    for status in (401, 402, 404):
        http.calls.clear()
        http.script[:] = [_or_err(status), _or_ok()]
        with pytest.raises(LLMError, match=f"HTTP {status}"):
            OpenRouterLLM("m").complete("s", "u")
        assert len(http.calls) == 1


def test_http_retries_exhausted(http, monkeypatch):
    monkeypatch.setenv("PMBOT_LLM_MAX_ATTEMPTS", "2")
    http.script[:] = [_or_err(502), _or_err(502)]
    with pytest.raises(LLMError, match="after 2 attempts"):
        OpenRouterLLM("m").complete("s", "u")


def test_openrouter_bad_bodies(http, monkeypatch):
    monkeypatch.setenv("PMBOT_LLM_MAX_ATTEMPTS", "1")
    http.script[:] = [_Resp(200, None, "<html>")]
    with pytest.raises(LLMError, match="non-JSON"):
        OpenRouterLLM("m").complete("s", "u")
    http.script[:] = [_Resp(200, {"error": {"code": 429, "message": "slow"}})]
    with pytest.raises(LLMError, match="HTTP 429"):
        OpenRouterLLM("m").complete("s", "u")
    http.script[:] = [_Resp(200, {"choices": []})]
    with pytest.raises(LLMError, match="choices"):
        OpenRouterLLM("m").complete("s", "u")


def test_httpllm_hooks_template(http):
    class _H(HttpLLM):
        provider_name, api_key_env = "h", "H_KEY"

        def _url(self):
            return "http://x/y"

        def _headers(self):
            return {"k": "v"}

        def _body(self, system, user, json_mode, web):
            return {"s": system, "j": json_mode}

        def _parse(self, data):
            return LLMResponse(data["t"], "h", self.model)

    http.script[:] = [_Resp(200, {"t": "ok"})]
    assert _H("m").complete("S", "U", json_mode=True).text == "ok"
    assert http.calls[0]["url"] == "http://x/y" and http.calls[0]["json"] == {"s": "S", "j": True}
    assert isinstance(HttpStatusError(400, "b"), Exception) and HttpStatusError(400, "b").status == 400


def test_openrouter_provider_ignore_env(http, monkeypatch):
    http.script[:] = [_or_ok(), _or_ok(), _or_ok()]
    OpenRouterLLM("m").complete("s", "u")
    assert http.calls[0]["json"]["provider"] == {"ignore": ["Alibaba"]}  # default
    monkeypatch.setenv("OPENROUTER_PROVIDER_IGNORE", " Alibaba , Foo ,")
    OpenRouterLLM("m").complete("s", "u")
    assert http.calls[1]["json"]["provider"] == {"ignore": ["Alibaba", "Foo"]}
    monkeypatch.setenv("OPENROUTER_PROVIDER_IGNORE", "")
    OpenRouterLLM("m").complete("s", "u")
    assert "provider" not in http.calls[2]["json"]  # empty = ignore none


def test_openrouter_inappropriate_content_403_retries_once(http):
    msg = "Upstream error from Alibaba: Output data may contain inappropriate content"
    http.script[:] = [_or_err(403, msg), _or_ok("fine")]
    assert OpenRouterLLM("m").complete("s", "u").text == "fine"
    assert len(http.calls) == 2
    http.calls.clear()
    http.script[:] = [_or_err(403, msg), _or_err(403, msg)]
    with pytest.raises(LLMError, match="inappropriate content"):
        OpenRouterLLM("m").complete("s", "u")
    assert len(http.calls) == 2  # one retry only
    http.calls.clear()
    http.script[:] = [_or_err(403, "forbidden key")]
    with pytest.raises(LLMError):
        OpenRouterLLM("m").complete("s", "u")
    assert len(http.calls) == 1  # other 403s are not retried
