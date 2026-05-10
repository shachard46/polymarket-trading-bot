"""F4 regression: agent.yaml schemas are enforced at the Hub boundary."""

from __future__ import annotations

import pytest

from orchestrator.runner import spawn_agent
from orchestrator.schema_validation import (
    AgentSchemaError,
    build_model,
    validate_payload,
)


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
    # Stub mode: the empty-string return is fine; we want to prove that
    # a malformed Hub payload never makes it to the SDK at all.
    with pytest.raises(AgentSchemaError):
        spawn_agent("evaluator", {"market_id": "x"})  # missing historic_market_data


def test_spawn_agent_accepts_valid_input(monkeypatch):
    # Schema-valid input → schema-valid stub response.
    out = spawn_agent(
        "evaluator",
        {"market_id": "x", "historic_market_data": []},
    )
    assert isinstance(out, dict)
    assert out["market_id"] == "x"
    assert out["error"] is None


def test_spawn_agent_re_evaluator_requires_schema_fields():
    with pytest.raises(AgentSchemaError):
        spawn_agent(
            "re_evaluator",
            {"market_id": "x", "historic_market_data": []},
        )


def test_spawn_agent_re_evaluator_accepts_quantitative_payload():
    out = spawn_agent(
        "re_evaluator",
        {
            "market_id": "x",
            "review_kind": "quantitative",
            "historic_market_data": [],
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


def test_spawn_agent_re_evaluator_accepts_edge_research_refresh_payload():
    out = spawn_agent(
        "re_evaluator",
        {
            "market_id": "x",
            "review_kind": "edge_research_refresh",
            "historic_market_data": [],
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
