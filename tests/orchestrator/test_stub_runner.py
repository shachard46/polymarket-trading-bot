"""F7 regression: stub runner returns schema-valid responses; stub_error drives inactive flagging."""

from __future__ import annotations

import json

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
        },
    )
    parsed = out if isinstance(out, dict) else parse_agent_json_or_yaml(out)
    assert len(parsed["research_queries"]) == 4
    assert all("as of 2026-10-22" in q for q in parsed["research_queries"])
    assert any("Will X?" in q for q in parsed["research_queries"])
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
        },
    )
    parsed = parse_deep_researcher_json(out)
    research = parse_deep_researcher(parsed.markdown)
    assert research.error == "stub_error mode"


def test_live_session_key_scopes_samples():
    base = runner._live_session_key("agent-x", "deep_researcher", {"market_id": "0xabc"})
    s2 = runner._live_session_key(
        "agent-x", "deep_researcher", {"market_id": "0xabc", "session_tag": "s2"}
    )
    n = runner.get_run_nonce()
    assert base == f"agent:agent-x:orch-{n}-0xabc"
    assert s2 == f"agent:agent-x:orch-{n}-0xabc-s2"


@pytest.mark.parametrize(
    ("market_id", "key_suffix"),
    [("0xabc", "0xabc"), ("market with spaces", "market-with-spaces")],
)
def test_live_runner_invokes_openclaw_cli(monkeypatch, market_id, key_suffix):
    calls = {}

    def fake_run_agent(agent_id, message, *, session_key, timeout=None):
        calls["agent_id"] = agent_id
        calls["message"] = message
        calls["session_key"] = session_key
        calls["timeout"] = timeout
        return {
            "payloads": [
                {
                    "text": json.dumps(
                        {
                            "market_id": market_id,
                            "researchable": False,
                            "category": "sports",
                            "manipulation_risk": "low",
                            "manipulation_reason": "none",
                            "reason": "live fake",
                            "error": None,
                        }
                    )
                }
            ]
        }

    monkeypatch.setenv(RUNNER_MODE_ENV, RUNNER_MODE_LIVE)
    monkeypatch.setattr(runner, "run_agent", fake_run_agent)

    out = runner.spawn_agent("triage", {**_TRIAGE_IN, "market_id": market_id})

    parsed = parse_agent_json_or_yaml(out)
    assert parsed["reason"] == "live fake"
    assert calls["agent_id"] == "polymarket-triage"
    # the key is sanitized (no spaces) and scoped by the per-tick run nonce
    assert calls["session_key"] == f"agent:polymarket-triage:orch-{runner.get_run_nonce()}-{key_suffix}"
    assert "OpenClaw agent id: polymarket-triage" in calls["message"]
    assert "Input JSON" in calls["message"]
    assert calls["timeout"] is None
