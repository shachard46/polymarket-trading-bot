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
