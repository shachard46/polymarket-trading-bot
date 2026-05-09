"""F7 regression: stub runner returns schema-valid responses; stub_error drives DLQ."""

from __future__ import annotations

import pytest

from orchestrator import runner
from orchestrator.config import RUNNER_MODE_ENV
from orchestrator.parse import agent_error_reason, parse_agent_json_or_yaml
from orchestrator.research import parse_deep_researcher


@pytest.fixture(autouse=True)
def _clear_mode(monkeypatch):
    monkeypatch.delenv(RUNNER_MODE_ENV, raising=False)
    yield


def test_stub_evaluator_returns_schema_valid_response():
    out = runner.spawn_agent(
        "evaluator",
        {"market_id": "0xabc", "historic_market_data": []},
    )
    parsed = out if isinstance(out, dict) else parse_agent_json_or_yaml(out)
    assert parsed["market_id"] == "0xabc"
    assert parsed["passed"] is False
    assert parsed["error"] is None


def test_stub_briefer_includes_market_title():
    out = runner.spawn_agent(
        "briefer",
        {"market_id": "0xabc", "market_title": "Will X?", "market_description": ""},
    )
    parsed = out if isinstance(out, dict) else parse_agent_json_or_yaml(out)
    assert "Will X?" in parsed["summary"]
    assert parsed["error"] is None


def test_stub_deep_researcher_returns_parseable_markdown():
    out = runner.spawn_agent(
        "deep_researcher",
        {
            "market_id": "0xabc",
            "market_data": {},
            "context_summary": "x",
            "directives": "y",
        },
    )
    research = parse_deep_researcher(out)
    assert research.market_id == "0xabc"
    assert 0.0 <= research.estimated_p <= 1.0
    assert "## Bull Thesis" in research.body


def test_stub_error_mode_propagates_error_field(monkeypatch):
    monkeypatch.setenv(RUNNER_MODE_ENV, "stub_error")
    out = runner.spawn_agent(
        "evaluator",
        {"market_id": "0xabc", "historic_market_data": []},
    )
    parsed = out if isinstance(out, dict) else parse_agent_json_or_yaml(out)
    assert agent_error_reason(parsed) == "stub_error mode"


def test_stub_error_deep_researcher_carries_error_in_frontmatter(monkeypatch):
    monkeypatch.setenv(RUNNER_MODE_ENV, "stub_error")
    out = runner.spawn_agent(
        "deep_researcher",
        {
            "market_id": "0xabc",
            "market_data": {},
            "context_summary": "x",
            "directives": "y",
        },
    )
    research = parse_deep_researcher(out)
    assert research.error == "stub_error mode"
