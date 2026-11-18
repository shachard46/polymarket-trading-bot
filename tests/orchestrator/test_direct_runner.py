"""Direct (Anthropic API) runner backend: mocked client, no network."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

import agents_blueprint
from orchestrator import config, direct_runner, runner


class _APIConnectionError(Exception):
    pass


class _RateLimitError(Exception):
    pass


class _APIStatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class _Block:
    def __init__(self, type_: str, text: str = ""):
        self.type, self.text = type_, text


class _Msg:
    def __init__(self, blocks, stop_reason="end_turn"):
        self.content = blocks
        self.stop_reason = stop_reason


class _FakeClient:
    instances: list["_FakeClient"] = []
    script: list[Any] = []

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.calls: list[dict[str, Any]] = []
        self.messages = self
        _FakeClient.instances.append(self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = _FakeClient.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture()
def fake_anthropic(monkeypatch):
    mod = types.ModuleType("anthropic")
    mod.Anthropic = _FakeClient
    mod.APIConnectionError = _APIConnectionError
    mod.RateLimitError = _RateLimitError
    mod.APIStatusError = _APIStatusError
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("OPENCLAW_AGENT_RETRY_BACKOFF", "0")
    monkeypatch.setattr(direct_runner.time, "sleep", lambda s: None)
    _FakeClient.instances = []
    _FakeClient.script = []
    return mod


def _ok(text="{}"):
    return _Msg([_Block("server_tool_use"), _Block("text", text), _Block("text", "!")])


def _call(role: str, payload: dict[str, Any]):
    return direct_runner.run_direct(role, agents_blueprint.AGENTS[role], payload)


TRIAGE_PAYLOAD = {"market_id": "0x1", "title": "t", "description": "d", "today": "2026-01-01"}


def _triage_payload():
    spec = agents_blueprint.AGENTS["triage"]
    fields = spec["input_model"].model_fields
    return {k: TRIAGE_PAYLOAD.get(k, "x") for k in fields}


def test_system_prompt_cached_and_text_concatenated(fake_anthropic):
    _FakeClient.script = [_ok('{"a":1}')]
    out = _call("triage", _triage_payload())
    assert out == '{"a":1}!'
    kw = _FakeClient.instances[0].calls[0]
    system = kw["system"][0]
    assert system["cache_control"] == {"type": "ephemeral"}
    assert system["text"] == agents_blueprint.AGENTS["triage"]["system_prompt"]
    assert "tools" not in kw
    assert kw["model"] == "claude-haiku-5-5"
    assert "Input JSON" in kw["messages"][0]["content"]
    assert "OpenClaw agent id" not in kw["messages"][0]["content"]


def test_only_web_searcher_gets_tools(fake_anthropic):
    for role, spec in agents_blueprint.AGENTS.items():
        tools = direct_runner._tools_for(spec)
        if role == "web_searcher":
            assert {t["name"] for t in tools} == {"web_search", "web_fetch"}
            assert all(t["max_uses"] >= 1 for t in tools)
            assert {t["type"] for t in tools} == {
                direct_runner.WEB_SEARCH_TOOL_TYPE,
                direct_runner.WEB_FETCH_TOOL_TYPE,
            }
        else:
            assert tools == [], role


def test_web_searcher_call_sends_tools(fake_anthropic):
    _FakeClient.script = [_ok()]
    _call("web_searcher", {"market_id": "0x1", "queries": ["q"], "today": "2026-01-01"})
    assert _FakeClient.instances[0].calls[0]["tools"]


def test_pause_turn_continues_and_returns_final_text_only(fake_anthropic):
    paused = _Msg([_Block("text", "searching...")], stop_reason="pause_turn")
    _FakeClient.script = [paused, _Msg([_Block("text", '{"final":1}')])]
    out = _call("web_searcher", {"market_id": "0x1", "queries": ["q"], "today": "2026-01-01"})
    assert out == '{"final":1}'
    calls = _FakeClient.instances[0].calls
    assert len(calls) == 2
    assert calls[1]["messages"][-1] == {"role": "assistant", "content": paused.content}


def test_pause_turn_capped_at_three_continuations(fake_anthropic):
    _FakeClient.script = [
        _Msg([_Block("text", f"t{i}")], stop_reason="pause_turn") for i in range(5)
    ]
    out = _call("web_searcher", {"market_id": "0x1", "queries": ["q"], "today": "2026-01-01"})
    assert len(_FakeClient.instances[0].calls) == 4
    assert out == "t3"


def test_empty_final_text_falls_back_to_all_text(fake_anthropic):
    _FakeClient.script = [
        _Msg([_Block("text", "partial")], stop_reason="pause_turn"),
        _Msg([_Block("server_tool_use")]),
    ]
    out = _call("web_searcher", {"market_id": "0x1", "queries": ["q"], "today": "2026-01-01"})
    assert out == "partial"


def test_retries_transport_errors_then_succeeds(fake_anthropic, monkeypatch):
    monkeypatch.setenv("OPENCLAW_AGENT_MAX_ATTEMPTS", "3")
    _FakeClient.script = [_APIConnectionError("x"), _APIStatusError(503), _ok("fine")]
    assert _call("triage", _triage_payload()) == "fine!"
    assert _FakeClient.instances[0].init_kwargs == {"max_retries": 0}
    assert len(_FakeClient.instances[0].calls) == 3


def test_retries_exhausted_raises(fake_anthropic, monkeypatch):
    monkeypatch.setenv("OPENCLAW_AGENT_MAX_ATTEMPTS", "2")
    _FakeClient.script = [_RateLimitError("a"), _RateLimitError("b")]
    with pytest.raises(direct_runner.DirectRunnerError):
        _call("triage", _triage_payload())
    assert len(_FakeClient.instances[0].calls) == 2


def test_client_error_not_retried(fake_anthropic):
    _FakeClient.script = [_APIStatusError(400), _ok()]
    with pytest.raises(_APIStatusError):
        _call("triage", _triage_payload())
    assert len(_FakeClient.instances[0].calls) == 1


def test_require_api_key(fake_anthropic, monkeypatch):
    direct_runner.require_api_key()
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(direct_runner.DirectRunnerError, match="ANTHROPIC_API_KEY"):
        direct_runner.require_api_key()


def test_missing_sdk_is_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import raises ImportError
    with pytest.raises(direct_runner.DirectRunnerError, match="pip install anthropic"):
        direct_runner.require_api_key()


def test_spawn_agent_routes_direct_mode(fake_anthropic, monkeypatch):
    monkeypatch.setenv(config.RUNNER_MODE_ENV, "direct")
    seen = {}

    def fake_run_direct(role, spec, payload):
        seen["role"] = role
        return ""

    monkeypatch.setattr(direct_runner, "run_direct", fake_run_direct)
    runner.spawn_agent("triage", _triage_payload())
    assert seen["role"] == "triage"


def test_model_per_role_and_env_override(monkeypatch):
    assert config.direct_model("triage") == "claude-haiku-5-5"
    assert config.direct_model("overseer") == "claude-opus-5-5"
    assert config.direct_model("deep_researcher") == "claude-sonnet-5-5"
    monkeypatch.setenv("DIRECT_MODEL_TRIAGE", "my-model")
    assert config.direct_model("triage") == "my-model"


def test_direct_is_valid_mode(monkeypatch):
    monkeypatch.setenv(config.RUNNER_MODE_ENV, "direct")
    assert config.runner_mode() == "direct"


def test_session_key_differs_across_nonces():
    old = runner.get_run_nonce()
    try:
        runner.set_run_nonce("tick1")
        a = runner._live_session_key("ag", "deep_researcher", {"market_id": "0x1"})
        o1 = runner._live_session_key("ag", "overseer", {})
        runner.set_run_nonce("tick2")
        b = runner._live_session_key("ag", "deep_researcher", {"market_id": "0x1"})
        o2 = runner._live_session_key("ag", "overseer", {})
        s2 = runner._live_session_key(
            "ag", "deep_researcher", {"market_id": "0x1", "session_tag": "s2"}
        )
        assert a != b and o1 != o2
        assert s2 != b
    finally:
        runner.set_run_nonce(old)


def test_ledger_fields_backend_and_model(monkeypatch):
    from orchestrator.phases.common import _ledger_market_fields

    monkeypatch.setenv(config.RUNNER_MODE_ENV, "stub")
    f = _ledger_market_fields({})
    assert f["runner_backend"] == "stub" and "model" not in f
    monkeypatch.setenv(config.RUNNER_MODE_ENV, "direct")
    assert "model" not in _ledger_market_fields({})
    f = _ledger_market_fields({}, role="triage")
    assert f["runner_backend"] == "direct" and f["model"] == "claude-haiku-5-5"


# ---------------------------------------------------------------------------
# OpenRouter provider


class _Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._data, self.text = status, data, text

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


def _or_ok(text="{}"):
    return _Resp(200, {"choices": [{"message": {"role": "assistant", "content": text}}]})


def _or_err(status, msg="nope"):
    return _Resp(status, {"error": {"code": status, "message": msg}})


@pytest.fixture()
def or_env(monkeypatch):
    monkeypatch.setenv("DIRECT_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    monkeypatch.setenv("DIRECT_MODEL_DEFAULT", "vendor/cheap-model")
    monkeypatch.setenv("OPENCLAW_AGENT_RETRY_BACKOFF", "0")
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    monkeypatch.setattr(direct_runner.time, "sleep", lambda s: None)
    calls: list[dict[str, Any]] = []
    script: list[Any] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(direct_runner.requests, "post", fake_post)
    return types.SimpleNamespace(calls=calls, script=script)


def test_openrouter_payload_shape(or_env):
    or_env.script[:] = [_or_ok('{"a":1}')]
    out = _call("triage", _triage_payload())
    assert out == '{"a":1}'
    c = or_env.calls[0]
    assert c["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert c["headers"]["Authorization"] == "Bearer or-test"
    body = c["json"]
    assert body["model"] == "vendor/cheap-model"
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][0]["content"] == agents_blueprint.AGENTS["triage"]["system_prompt"]
    assert body["response_format"] == {"type": "json_object"}
    assert "plugins" not in body
    assert "HTTP-Referer" not in c["headers"] and "X-Title" not in c["headers"]


def test_openrouter_optional_headers_and_base_url(or_env, monkeypatch):
    monkeypatch.setenv("OPENROUTER_HTTP_REFERER", "https://example.test")
    monkeypatch.setenv("OPENROUTER_APP_TITLE", "bot")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "http://proxy/v1/")
    or_env.script[:] = [_or_ok()]
    _call("triage", _triage_payload())
    c = or_env.calls[0]
    assert c["url"] == "http://proxy/v1/chat/completions"
    assert c["headers"]["HTTP-Referer"] == "https://example.test"
    assert c["headers"]["X-Title"] == "bot"


def test_openrouter_web_plugin_only_for_web_searcher(or_env):
    or_env.script[:] = [_or_ok(), _or_ok()]
    _call("web_searcher", {"market_id": "0x1", "queries": ["q"], "today": "2026-01-01"})
    _call("triage", _triage_payload())
    assert or_env.calls[0]["json"]["plugins"] == [
        {"id": "web", "max_results": config.direct_web_max_uses()}
    ]
    assert "plugins" not in or_env.calls[1]["json"]


def test_openrouter_json_object_400_falls_back(or_env):
    or_env.script[:] = [_or_err(400, "response_format unsupported"), _or_ok("fine")]
    assert _call("triage", _triage_payload()) == "fine"
    assert "response_format" in or_env.calls[0]["json"]
    assert "response_format" not in or_env.calls[1]["json"]


def test_openrouter_400_without_response_format_raises(or_env):
    or_env.script[:] = [_or_err(400)]
    with pytest.raises(direct_runner.DirectRunnerError, match="HTTP 400"):
        direct_runner.complete("concierge", "sys", "hi")
    assert len(or_env.calls) == 1


def test_openrouter_retry_classification(or_env, monkeypatch):
    monkeypatch.setenv("OPENCLAW_AGENT_MAX_ATTEMPTS", "4")
    or_env.script[:] = [
        direct_runner.requests.ConnectionError("x"), _or_err(429), _or_err(503), _or_ok("ok"),
    ]
    assert direct_runner.complete("concierge", "sys", "hi") == "ok"
    assert len(or_env.calls) == 4


def test_openrouter_non_retryable_4xx(or_env, monkeypatch):
    monkeypatch.setenv("OPENCLAW_AGENT_MAX_ATTEMPTS", "4")
    for status in (401, 402, 404):
        or_env.calls.clear()
        or_env.script[:] = [_or_err(status), _or_ok()]
        with pytest.raises(direct_runner.DirectRunnerError, match=f"HTTP {status}"):
            direct_runner.complete("concierge", "sys", "hi")
        assert len(or_env.calls) == 1


def test_openrouter_retries_exhausted(or_env, monkeypatch):
    monkeypatch.setenv("OPENCLAW_AGENT_MAX_ATTEMPTS", "2")
    or_env.script[:] = [_or_err(502), _or_err(502)]
    with pytest.raises(direct_runner.DirectRunnerError, match="after 2 attempts"):
        direct_runner.complete("concierge", "sys", "hi")


def test_openrouter_does_not_need_anthropic_sdk(or_env, monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    direct_runner.require_api_key()
    or_env.script[:] = [_or_ok("hi")]
    assert direct_runner.complete("concierge", "sys", "u") == "hi"


def test_complete_openrouter_no_tools_no_json(or_env):
    or_env.script[:] = [_or_ok("answer")]
    assert direct_runner.complete("concierge", "SYS", "USER") == "answer"
    body = or_env.calls[0]["json"]
    assert body["messages"] == [
        {"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"},
    ]
    assert "response_format" not in body and "plugins" not in body


def test_complete_anthropic(fake_anthropic):
    _FakeClient.script = [_ok("x")]
    assert direct_runner.complete("concierge", "SYS", "USER") == "x!"
    kw = _FakeClient.instances[0].calls[0]
    assert kw["system"][0]["text"] == "SYS"
    assert kw["messages"] == [{"role": "user", "content": "USER"}]
    assert "tools" not in kw and kw["model"] == "claude-sonnet-5-5"


def test_require_api_key_openrouter(or_env, monkeypatch):
    direct_runner.require_api_key()  # default model covers every Hub role
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(direct_runner.DirectRunnerError, match="OPENROUTER_API_KEY"):
        direct_runner.require_api_key()


def test_require_api_key_openrouter_missing_model_lists_hub_roles(or_env, monkeypatch):
    monkeypatch.delenv("DIRECT_MODEL_DEFAULT")
    monkeypatch.setenv("DIRECT_MODEL_TRIAGE", "m")
    with pytest.raises(direct_runner.DirectRunnerError) as ei:
        direct_runner.require_api_key()
    msg = str(ei.value)
    assert "deep_researcher" in msg and "overseer" in msg
    assert "triage" not in msg.split("role(s)")[1].split(":")[0]
    assert "concierge" not in msg


def test_require_api_key_openrouter_concierge_not_required(or_env, monkeypatch):
    monkeypatch.delenv("DIRECT_MODEL_DEFAULT")
    for role, spec in agents_blueprint.AGENTS.items():
        if spec["hub_spawned"]:
            monkeypatch.setenv(f"DIRECT_MODEL_{role.upper()}", "m")
    direct_runner.require_api_key()


def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("DIRECT_PROVIDER", "bogus")
    with pytest.raises(direct_runner.DirectRunnerError, match="DIRECT_PROVIDER"):
        direct_runner.require_api_key()


def test_direct_model_resolution(monkeypatch):
    monkeypatch.delenv("DIRECT_PROVIDER", raising=False)
    monkeypatch.setenv("DIRECT_MODEL_DEFAULT", "dflt")
    assert config.direct_model("triage") == "claude-haiku-5-5"  # built-in role default wins
    assert config.direct_model("concierge") == "dflt"
    monkeypatch.setenv("DIRECT_PROVIDER", "openrouter")
    assert config.direct_model("triage") == "dflt"
    monkeypatch.setenv("DIRECT_MODEL_TRIAGE", "mine")
    assert config.direct_model("triage") == "mine"
    monkeypatch.delenv("DIRECT_MODEL_DEFAULT")
    with pytest.raises(ValueError):
        config.direct_model("overseer")


def test_ledger_fields_provider_and_live_label(monkeypatch):
    from orchestrator.phases.common import _ledger_market_fields

    monkeypatch.setenv(config.RUNNER_MODE_ENV, "direct")
    monkeypatch.setenv("DIRECT_PROVIDER", "openrouter")
    monkeypatch.setenv("DIRECT_MODEL_DEFAULT", "vendor/m")
    f = _ledger_market_fields({}, role="deep_researcher")
    assert f["provider"] == "openrouter" and f["model"] == "vendor/m"
    assert "provider" not in _ledger_market_fields({})
    monkeypatch.setenv(config.RUNNER_MODE_ENV, "live")
    monkeypatch.setenv("OPENCLAW_MODEL_LABEL", "claw-x")
    f = _ledger_market_fields({}, role="triage")
    assert f["model"] == "claw-x" and "provider" not in f
    monkeypatch.delenv("OPENCLAW_MODEL_LABEL")
    assert "model" not in _ledger_market_fields({}, role="triage")


def test_stats_brier_by_model():
    from orchestrator import ledger
    from orchestrator.stats import compute_stats, render_stats

    rows = [
        {"market_id": "a", "stage": "traded", "p": 0.8, "q": 0.5, "model": "m1", "ts": "2026-01-01T00:00:00Z"},
        {"market_id": "b", "stage": "traded", "p": 0.2, "q": 0.5, "model": "m2", "ts": "2026-01-01T00:00:00Z"},
        {"market_id": "a", "stage": ledger.RESOLUTION_STAGE, "outcome_yes": 1, "ts": "2026-01-02T00:00:00Z"},
        {"market_id": "b", "stage": ledger.RESOLUTION_STAGE, "outcome_yes": 1, "ts": "2026-01-02T00:00:00Z"},
    ]
    s = compute_stats(rows)
    by = s["brier_by_model"]
    assert by["m1"][0] == 1 and by["m1"][1] == pytest.approx(0.04)
    assert by["m2"][1] == pytest.approx(0.64)
    out = render_stats(s)
    assert "### Brier by model" in out and "m1" in out
