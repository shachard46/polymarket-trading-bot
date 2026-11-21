"""claude_code provider (Claude Agent SDK on the subscription): SDK mocked, no subprocess, no network."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from orchestrator.llm import usage
from orchestrator.llm.base import LLMError, LLMResponse
from orchestrator.llm.claude_code_llm import ClaudeCodeLLM
from orchestrator.llm.registry import PROVIDERS


class _Result:
    def __init__(self, result="ok", is_error=False, stop_reason="end_turn", status=None, cost=0.05,
                 usage_=None, subtype="success"):
        self.result, self.is_error, self.stop_reason, self.api_error_status = result, is_error, stop_reason, status
        self.total_cost_usd, self.usage, self.subtype = cost, usage_ or {"input_tokens": 10, "output_tokens": 5,
                                                                         "cache_read_input_tokens": 100}, subtype


class _Msg:
    def __init__(self, content):
        self.content = content


class _Use:
    def __init__(self, url):
        self.input = {"url": url}


class _ToolResult:
    def __init__(self, content):
        self.content = content


@pytest.fixture()
def sdk(monkeypatch):
    mod = types.ModuleType("claude_agent_sdk")
    mod.ResultMessage, mod.UserMessage, mod.AssistantMessage = _Result, _Msg, type("A", (_Msg,), {})
    mod.ToolResultBlock, mod.ToolUseBlock = _ToolResult, _Use

    class Options:
        def __init__(self, **kw):
            self.kw = kw

    class Conn(Exception):
        pass

    mod.ClaudeAgentOptions, mod.CLIConnectionError = Options, Conn
    mod.calls, mod.script = [], []

    async def query(*, prompt, options):
        mod.calls.append((prompt, options.kw))
        item = mod.script.pop(0)
        if isinstance(item, Exception):
            raise item
        for m in item:
            yield m

    mod.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-used")
    monkeypatch.setenv("PMBOT_LLM_RETRY_BACKOFF", "0")
    monkeypatch.setattr("orchestrator.llm.base.time.sleep", lambda s: None)
    return mod


def test_registered_and_check_key(sdk, monkeypatch):
    assert PROVIDERS["claude_code"] is ClaudeCodeLLM
    ClaudeCodeLLM.check_key()
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    with pytest.raises(LLMError, match="CLAUDE_CODE_OAUTH_TOKEN"):
        ClaudeCodeLLM.check_key()
    assert not ClaudeCodeLLM.supports_batch


def test_options_no_web_isolated_and_no_api_key(sdk):
    sdk.script = [[_Result("answer")]]
    llm = ClaudeCodeLLM("claude-sonnet-5-5")
    r = llm.complete("SYS", "USER")
    assert (r.text, r.provider, r.model, r.citations) == ("answer", "claude_code", "claude-sonnet-5-5", None)
    assert (r.input_tokens, r.output_tokens, r.notional_cost_usd) == (110, 5, 0.05)
    prompt, kw = sdk.calls[0]
    assert prompt == "USER" and kw["system_prompt"] == "SYS" and kw["model"] == "claude-sonnet-5-5"
    assert kw["setting_sources"] == [] and kw["tools"] == [] and kw["allowed_tools"] == [] and kw["max_turns"] == 1
    assert kw["permission_mode"] == "dontAsk" and "effort" not in kw
    assert kw["env"]["ANTHROPIC_API_KEY"] == "" and kw["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"


def test_web_tools_effort_and_citations(sdk):
    sdk.script = [[_Msg([_Use("https://a.example/x")]),
                   _Msg([_ToolResult([{"type": "text", "text": "see https://b.example/y and https://a.example/x"}])]),
                   _Result("- f | https://b.example/y | 2026-01-01")]]
    llm = ClaudeCodeLLM("m", web_max_uses=3)
    llm.effort = "medium"
    r = llm.complete("s", "u", web=True)
    kw = sdk.calls[0][1]
    assert kw["tools"] == kw["allowed_tools"] == ["WebSearch", "WebFetch"] and kw["effort"] == "medium"
    assert kw["max_turns"] > 1
    assert r.citations == ["https://a.example/x", "https://b.example/y"]


def test_refusal_and_error_are_not_retried(sdk, monkeypatch):
    monkeypatch.setenv("PMBOT_LLM_MAX_ATTEMPTS", "3")
    sdk.script = [[_Result("", stop_reason="refusal")], [_Result("no", is_error=True)]]
    for match in ("refus", "call failed"):
        with pytest.raises(LLMError, match=match):
            ClaudeCodeLLM("m").complete("s", "u")
    assert len(sdk.calls) == 2


def test_transient_status_and_connection_errors_retry(sdk, monkeypatch):
    monkeypatch.setenv("PMBOT_LLM_MAX_ATTEMPTS", "3")
    sdk.script = [[_Result("busy", is_error=True, status=429)], sdk.CLIConnectionError("x"), [_Result("fine")]]
    assert ClaudeCodeLLM("m").complete("s", "u").text == "fine"
    assert len(sdk.calls) == 3


def test_usage_row_billed_zero_with_notional(sdk, monkeypatch, tmp_path):
    monkeypatch.setenv("PMBOT_VAULT_PATH", str(tmp_path))
    sdk.script = [[_Result("x", cost=0.07)]]
    ClaudeCodeLLM("m").complete("s", "u")
    rows = usage.iter_usage(tmp_path)
    assert rows[0]["cost_usd"] == 0.0 and rows[0]["notional_cost_usd"] == 0.07 and rows[0]["provider"] == "claude_code"


def test_stats_arm_cost_uses_notional():
    from orchestrator.stats.cost import judge_model_arm_cost, judge_model_arm_web_cost

    rows = [{"market_id": "a", "judge_model_arm": "sonnet"}]
    use = [{"market_id": "a", "judge_arm": "one", "cost_usd": 0.0, "notional_cost_usd": 0.2, "role": "deep_researcher"},
           {"market_id": "a", "cost_usd": 0.0, "notional_cost_usd": 0.1, "role": "web_searcher"},
           {"market_id": "a", "cost_usd": 0.05, "role": "web_extractor"}]
    assert judge_model_arm_cost(rows, use)["sonnet"] == (1, pytest.approx(0.2), pytest.approx(0.2))
    assert judge_model_arm_web_cost(rows, use)["sonnet"] == (1, pytest.approx(0.15), pytest.approx(0.15))
