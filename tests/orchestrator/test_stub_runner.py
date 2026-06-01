"""F7 regression: stub runner returns schema-valid responses; stub_error drives inactive flagging."""

from __future__ import annotations

import pytest

from config.trading_constants import FILTERS
from orchestrator import runner
from orchestrator.config import RUNNER_MODE_ENV, RUNNER_MODE_LIVE
from orchestrator.parse import agent_error_reason, parse_agent_json_or_yaml
from orchestrator.agent_outputs import parse_deep_researcher_json
from orchestrator.research import parse_deep_researcher

_EVALUATOR_IN = {"market_id": "0xabc", "filter_directives": dict(FILTERS)}
_RE_EVALUATOR_IN = {
    "market_id": "0xabc",
    "review_kind": "quantitative",
    "filter_directives": dict(FILTERS),
    "historic_signal_bundle": None,
    "prior_filter_trigger": None,
    "prior_evaluator_details": None,
    "prior_filter_log": None,
    "research_markdown": None,
    "trade_log": None,
}


@pytest.fixture(autouse=True)
def _clear_mode(monkeypatch):
    monkeypatch.delenv(RUNNER_MODE_ENV, raising=False)
    yield


def test_stub_evaluator_returns_schema_valid_response():
    out = runner.spawn_agent("evaluator", _EVALUATOR_IN)
    parsed = out if isinstance(out, dict) else parse_agent_json_or_yaml(out)
    assert parsed["market_id"] == "0xabc"
    assert parsed["passed"] is False
    assert parsed["error"] is None
    assert "signal_bundle" not in parsed


def test_stub_briefer_includes_market_title():
    out = runner.spawn_agent(
        "briefer",
        {
            "market_id": "0xabc",
            "market_title": "Will X?",
            "market_description": "",
            "planning_context": None,
        },
    )
    parsed = out if isinstance(out, dict) else parse_agent_json_or_yaml(out)
    assert any("Will X?" in q for q in parsed["research_queries"])
    assert parsed["error"] is None


def test_stub_deep_researcher_returns_parseable_complete_payload():
    out = runner.spawn_agent(
        "deep_researcher",
        {
            "market_id": "0xabc",
            "market_data": {},
            "directives": "y",
            "research_bundle": [],
            "system_override": None,
            "format_validation_error": None,
        },
    )
    parsed = parse_deep_researcher_json(out)
    assert parsed.status == "complete"
    research = parse_deep_researcher(parsed.markdown)
    assert research.market_id == "0xabc"
    assert 0.0 <= research.estimated_p <= 1.0
    assert "## Bull Thesis" in research.body


def test_stub_error_mode_propagates_error_field(monkeypatch):
    monkeypatch.setenv(RUNNER_MODE_ENV, "stub_error")
    out = runner.spawn_agent("evaluator", _EVALUATOR_IN)
    parsed = out if isinstance(out, dict) else parse_agent_json_or_yaml(out)
    assert agent_error_reason(parsed) == "stub_error mode"


def test_stub_error_deep_researcher_carries_error_in_frontmatter(monkeypatch):
    monkeypatch.setenv(RUNNER_MODE_ENV, "stub_error")
    out = runner.spawn_agent(
        "deep_researcher",
        {
            "market_id": "0xabc",
            "market_data": {},
            "directives": "y",
            "research_bundle": [],
            "system_override": None,
            "format_validation_error": None,
        },
    )
    parsed = parse_deep_researcher_json(out)
    research = parse_deep_researcher(parsed.markdown)
    assert research.error == "stub_error mode"


def test_stub_re_evaluator_returns_refresh_fields():
    out = runner.spawn_agent("re_evaluator", _RE_EVALUATOR_IN)
    parsed = out if isinstance(out, dict) else parse_agent_json_or_yaml(out)
    assert parsed["retry_deep_research"] is False
    assert parsed["refresh_reason"] is None
    assert "signal_bundle" not in parsed


def test_stub_executioner_includes_allocation_tool_fields():
    out = runner.spawn_agent(
        "executioner",
        {
            "market_id": "0xabc",
            "p_value": 0.5,
            "market_data": {},
            "paper_trade_mode": True,
        },
    )
    parsed = out if isinstance(out, dict) else parse_agent_json_or_yaml(out)
    assert parsed["score"] == 0.0
    assert parsed["below_edge_threshold"] is True
    assert parsed["allocation_usd"] == 0.0


def test_live_runner_invokes_openclaw_cli(monkeypatch):
    calls = {}

    def fake_run_agent(agent_id, message, *, session_key, timeout=None):
        calls["agent_id"] = agent_id
        calls["message"] = message
        calls["session_key"] = session_key
        calls["timeout"] = timeout
        return {
            "payloads": [
                {
                    "text": (
                        '{"market_id":"0xabc","passed":false,"trigger":null,'
                        '"confidence_multiplier":1.0,"details":"live fake",'
                        '"error":null}'
                    )
                }
            ]
        }

    monkeypatch.setenv(RUNNER_MODE_ENV, RUNNER_MODE_LIVE)
    monkeypatch.setattr(runner, "run_agent", fake_run_agent)

    out = runner.spawn_agent("evaluator", _EVALUATOR_IN)

    parsed = parse_agent_json_or_yaml(out)
    assert parsed["details"] == "live fake"
    assert calls["agent_id"] == "polymarket-evaluator"
    assert calls["session_key"] == "agent:polymarket-evaluator:orch-0xabc"
    assert "OpenClaw agent id: polymarket-evaluator" in calls["message"]
    assert "Input JSON" in calls["message"]
    assert calls["timeout"] is None


def test_live_runner_sanitizes_session_key(monkeypatch):
    calls = {}

    def fake_run_agent(agent_id, message, *, session_key, timeout=None):
        calls["session_key"] = session_key
        return {
            "payloads": [
                {
                    "text": (
                        '{"market_id":"market with spaces","passed":false,'
                        '"trigger":null,"confidence_multiplier":1.0,'
                        '"details":"live fake","error":null}'
                    )
                }
            ]
        }

    monkeypatch.setenv(RUNNER_MODE_ENV, RUNNER_MODE_LIVE)
    monkeypatch.setattr(runner, "run_agent", fake_run_agent)

    runner.spawn_agent(
        "evaluator",
        {"market_id": "market with spaces", "filter_directives": dict(FILTERS)},
    )

    assert calls["session_key"] == "agent:polymarket-evaluator:orch-market-with-spaces"
