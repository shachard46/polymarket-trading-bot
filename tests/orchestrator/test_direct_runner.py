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
