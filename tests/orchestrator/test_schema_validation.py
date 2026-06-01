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

Body.

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
        spawn_agent("evaluator", {"market_id": "x"})  # missing filter_directives


def test_spawn_agent_accepts_valid_input(monkeypatch):
    out = spawn_agent(
        "evaluator",
        {"market_id": "x", "filter_directives": dict(FILTERS)},
    )
    assert isinstance(out, dict)
    assert out["market_id"] == "x"
    assert out["error"] is None
    assert "signal_bundle" not in out


def test_spawn_agent_re_evaluator_requires_schema_fields():
    with pytest.raises(AgentSchemaError):
        spawn_agent(
            "re_evaluator",
            {"market_id": "x", "filter_directives": dict(FILTERS)},
        )


def test_spawn_agent_re_evaluator_accepts_quantitative_payload():
    out = spawn_agent(
        "re_evaluator",
        {
            "market_id": "x",
            "review_kind": "quantitative",
            "filter_directives": dict(FILTERS),
            "historic_signal_bundle": None,
            "prior_filter_trigger": None,
            "prior_evaluator_details": None,
            "prior_filter_log": None,
            "research_markdown": None,
            "trade_log": None,
        },
    )
    assert out["market_id"] == "x"
    assert out["error"] is None
    assert out["retry_deep_research"] is False
    assert "signal_bundle" not in out


def test_spawn_agent_re_evaluator_accepts_edge_research_refresh_payload():
    out = spawn_agent(
        "re_evaluator",
        {
            "market_id": "x",
            "review_kind": "edge_research_refresh",
            "filter_directives": dict(FILTERS),
            "historic_signal_bundle": {"signals": {}},
            "prior_filter_trigger": None,
            "prior_evaluator_details": None,
            "prior_filter_log": {"passed": True},
            "research_markdown": "---\nmarket_id: x\n---\n",
            "trade_log": {"below_edge_threshold": True},
        },
    )
    assert out["market_id"] == "x"
    assert out["refresh_reason"] == "no_material_quant_change"


def test_spawn_agent_executioner_requires_paper_trade_mode():
    with pytest.raises(AgentSchemaError):
        spawn_agent(
            "executioner",
            {"market_id": "m", "p_value": 0.5, "market_data": {}},
        )


def test_spawn_agent_executioner_accepts_paper_mode():
    out = spawn_agent(
        "executioner",
        {
            "market_id": "m",
            "p_value": 0.5,
            "market_data": {},
            "paper_trade_mode": True,
        },
    )
    assert out["market_id"] == "m"
    assert out["executed"] is False


def test_spawn_agent_overseer_accepts_valid_payload():
    out = spawn_agent(
        "overseer",
        {
            "post_mortems": [{"market_id": "m1", "content": "post-mortem body"}],
            "current_directives": _OVERSEER_DIRECTIVES,
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
            "market_title": "Title",
            "market_description": "",
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
                "market_title": "Title",
                "market_description": "",
                "planning_context": None,
            },
        )
    msg = str(exc_info.value)
    assert "research_queries" in msg


def test_spawn_agent_briefer_accepts_valid_payload():
    out = spawn_agent(
        "briefer",
        {
            "market_id": "0xabc",
            "market_title": "Will X happen?",
            "market_description": "",
            "planning_context": None,
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
