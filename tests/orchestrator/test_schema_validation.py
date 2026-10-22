"""F4 regression: agent.yaml schemas are enforced at the Hub boundary."""

from __future__ import annotations

import pytest

from agents_blueprint import AGENTS
from config.trading_constants import FILTERS
from orchestrator.live_prompt import build_live_response_hint
from orchestrator.runner import STUB_RESPONSES, _build_live_prompt, _live_session_key, spawn_agent
from orchestrator.schema_validation import (
    AgentSchemaError,
    build_model,
    validate_payload,
)

_OVERSEER_DIRECTIVES = """\
---
version: "1.0"
---

## Research Protocol

Body.

## Filter Weightings

```yaml
filters:
  min_liquidity: 1000
```

## Risk Constraints

Body.

## Output Requirements

Body.
"""


def test_build_model_resolves_nullable_and_primitives():
    Model = build_model(
        "T",
        {
            "market_id": "string",
            "passed": "boolean",
            "trigger": "string | null",
            "confidence_multiplier": "float",
            "details": "string",
            "error": "string | null",
        },
    )
    assert Model is not None
    Model.model_validate(
        {
            "market_id": "x",
            "passed": True,
            "trigger": None,
            "confidence_multiplier": 1.5,
            "details": "ok",
            "error": None,
        }
    )


def test_validate_payload_rejects_missing_required_field():
    Model = build_model("T", {"market_id": "string", "p_value": "float"})
    with pytest.raises(AgentSchemaError):
        validate_payload("test_role", "input", Model, {"market_id": "x"})


def test_spawn_agent_rejects_bad_input_before_calling_runner(monkeypatch):
    with pytest.raises(AgentSchemaError):
        spawn_agent("triage", {"market_id": "x"})  # missing title etc.


def test_spawn_agent_accepts_valid_input(monkeypatch):
    out = spawn_agent(
        "triage",
        {
            "market_id": "x",
            "title": "Will X?",
            "description": "",
            "resolution_source": None,
            "category": None,
            "end_date": None,
            "today": "2026-01-01",
        },
    )
    assert isinstance(out, dict)
    assert out["market_id"] == "x"
    assert out["error"] is None
    assert out["researchable"] is True


def test_triage_payload_is_price_blind():
    # Triage is price-blind by design: the Hub never sends a price.
    from orchestrator import phases
    from orchestrator.scraper import MarketRow

    payload = phases._triage_payload(
        MarketRow(market_id="x", market_title="T", market_data={"yes_price": 0.4}), "2026-01-01"
    )
    assert "yes_price" not in payload and "q" not in payload


def test_spawn_agent_overseer_accepts_valid_payload():
    out = spawn_agent(
        "overseer",
        {
            "post_mortems": [{"market_id": "m1", "content": "post-mortem body"}],
            "current_directives": _OVERSEER_DIRECTIVES,
            "stats_md": "",
            "failure_class_counts": {},
            "bounds": [],
        },
    )
    assert isinstance(out, dict)
    assert "new_directives_markdown" in out
    assert out["rationale"]
    assert out.get("error") is None
    assert "market_id" not in out


def test_spawn_agent_overseer_rejects_evaluator_shape(monkeypatch):
    def bad_overseer(_payload: dict) -> dict:
        return {
            "market_id": None,
            "passed": False,
            "trigger": None,
            "confidence_multiplier": 1.0,
            "details": "wrong shape",
            "error": None,
        }

    monkeypatch.setitem(STUB_RESPONSES, "overseer", bad_overseer)
    with pytest.raises(AgentSchemaError) as exc_info:
        spawn_agent(
            "overseer",
            {
                "post_mortems": [],
                "current_directives": _OVERSEER_DIRECTIVES,
                "stats_md": "",
                "failure_class_counts": {},
                "bounds": [],
            "stats_md": "",
            "failure_class_counts": {},
            "bounds": [],
            },
        )
    msg = str(exc_info.value)
    assert "new_directives_markdown" in msg or "rationale" in msg


def test_build_live_response_hint_lists_schema_keys():
    hint = build_live_response_hint(
        {
            "market_id": "string",
            "research_queries": "list[str]",
            "error": "string | null",
        }
    )
    assert "research_queries" in hint
    assert "error" in hint
    assert "decision fields" not in hint
    assert "nullable fields" in hint


def test_build_live_prompt_lists_briefer_schema_keys():
    spec = AGENTS["briefer"]
    prompt = _build_live_prompt(
        "briefer",
        spec,
        {
            "market_id": "0xabc",
            "title": "Title",
            "description": "",
            "resolution_source": None,
            "end_date": None,
            "category": None,
            "today": "2026-10-22",
        },
        agent_id="polymarket-briefer",
    )
    assert "research_queries" in prompt
    assert "error" in prompt
    assert "decision fields" not in prompt
    assert "Required keys exactly" in prompt


def test_build_live_prompt_overseer_includes_yaml_hint():
    spec = AGENTS["overseer"]
    prompt = _build_live_prompt(
        "overseer",
        spec,
        {"post_mortems": [], "current_directives": "x"},
        agent_id="polymarket-overseer",
    )
    assert "new_directives_markdown" in prompt
    assert "rationale" in prompt
    assert "## Research Protocol" in prompt
    assert "Do NOT include market_id" in prompt
    assert spec.get("live_response_hint")


def test_spawn_agent_briefer_rejects_evaluator_shape(monkeypatch):
    def bad_briefer(_payload: dict) -> dict:
        return {
            "market_id": _payload["market_id"],
            "passed": False,
            "trigger": None,
            "confidence_multiplier": 1.0,
            "details": "wrong shape",
            "error": None,
        }

    monkeypatch.setitem(STUB_RESPONSES, "briefer", bad_briefer)
    with pytest.raises(AgentSchemaError) as exc_info:
        spawn_agent(
            "briefer",
            {
                "market_id": "0xabc",
                "title": "Title",
                "description": "",
                "resolution_source": None,
                "end_date": None,
                "category": None,
                "today": "2026-10-22",
            },
        )
    msg = str(exc_info.value)
    assert "research_queries" in msg


def test_spawn_agent_briefer_accepts_valid_payload():
    out = spawn_agent(
        "briefer",
        {
            "market_id": "0xabc",
            "title": "Will X happen?",
            "description": "",
            "resolution_source": None,
            "end_date": None,
            "category": None,
            "today": "2026-10-22",
        },
    )
    assert out["market_id"] == "0xabc"
    assert out["research_queries"]
    assert out.get("error") is None


def test_live_session_key_overseer_is_isolated():
    key = _live_session_key(
        "polymarket-overseer",
        "overseer",
        {"post_mortems": [], "current_directives": "x"},
    )
    assert key == "agent:polymarket-overseer:orch-overseer-directives"
    assert "global" not in key
