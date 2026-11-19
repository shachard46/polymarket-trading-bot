"""F7 regression: stub runner returns schema-valid responses; stub_error drives inactive flagging."""

from __future__ import annotations

import pytest

from orchestrator import runner
from orchestrator.agent_outputs import parse_deep_researcher_json
from orchestrator.config import RUNNER_MODE_ENV, RUNNER_MODE_LIVE
from orchestrator.parse import agent_error_reason, parse_agent_json_or_yaml
from orchestrator.research import parse_deep_researcher

_TRIAGE_IN = {
    "market_id": "0xabc",
    "title": "Will X?",
    "description": "",
    "resolution_source": None,
    "category": None,
    "end_date": None,
    "today": "2026-01-01",
}


@pytest.fixture(autouse=True)
def _clear_mode(monkeypatch):
    monkeypatch.delenv(RUNNER_MODE_ENV, raising=False)
    yield


def test_stub_triage_returns_schema_valid_response():
    out = runner.spawn_agent("triage", _TRIAGE_IN)
    parsed = out if isinstance(out, dict) else parse_agent_json_or_yaml(out)
    assert parsed["market_id"] == "0xabc"
    assert parsed["researchable"] is True
    assert parsed["error"] is None


def test_stub_briefer_includes_market_title():
    out = runner.spawn_agent(
        "briefer",
        {
            "market_id": "0xabc",
            "title": "Will X?",
            "description": "",
            "resolution_source": None,
            "end_date": None,
            "category": None,
            "today": "2026-10-22",
            "depth": "full",
            "scout": None,
        },
    )
    parsed = out if isinstance(out, dict) else parse_agent_json_or_yaml(out)
    queries = [q for c in parsed["cruxes"] for q in c["queries"]]
    assert {c["role"] for c in parsed["cruxes"]} >= {"mechanism", "comparator", "horizon", "skeptic"}
    assert all("as of 2026-10-22" in q for q in queries)
    assert any("Will X?" in q for q in queries)
    assert parsed["error"] is None


def test_stub_deep_researcher_returns_parseable_complete_payload():
    out = runner.spawn_agent(
        "deep_researcher",
{
            "market_id": "0xabc",
            "title": "Will X?",
            "description": "",
            "resolution_source": None,
            "end_date": None,
            "category": None,
            "today": "2026-10-22",
            "mode": "full",
            "directives": "y",
            "research_bundle": [],
            "system_override": None,
            "format_validation_error": None,
            "session_tag": None,
            "ask": None,
            "crux_table": None,
            "known_unknowns": None,
        },
    )
    parsed = parse_deep_researcher_json(out)
    assert parsed.status == "complete"
    research = parse_deep_researcher(parsed.markdown)
    assert research.market_id == "0xabc"
    assert 0.0 <= research.estimated_p <= 1.0
    assert parsed.p_low <= parsed.estimated_p <= parsed.p_high
    assert parsed.confidence in ("low", "medium", "high")
    assert "## Bull Thesis" in research.body


def test_stub_error_mode_propagates_error_field(monkeypatch):
    monkeypatch.setenv(RUNNER_MODE_ENV, "stub_error")
    out = runner.spawn_agent("triage", _TRIAGE_IN)
    parsed = out if isinstance(out, dict) else parse_agent_json_or_yaml(out)
    assert agent_error_reason(parsed) == "stub_error mode"


def test_stub_error_deep_researcher_carries_error_in_frontmatter(monkeypatch):
    monkeypatch.setenv(RUNNER_MODE_ENV, "stub_error")
    out = runner.spawn_agent(
        "deep_researcher",
{
            "market_id": "0xabc",
            "title": "Will X?",
            "description": "",
            "resolution_source": None,
            "end_date": None,
            "category": None,
            "today": "2026-10-22",
            "mode": "full",
            "directives": "y",
            "research_bundle": [],
            "system_override": None,
            "format_validation_error": None,
            "session_tag": None,
            "ask": None,
            "crux_table": None,
            "known_unknowns": None,
        },
    )
    parsed = parse_deep_researcher_json(out)
    research = parse_deep_researcher(parsed.markdown)
    assert research.error == "stub_error mode"
