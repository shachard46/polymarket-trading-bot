"""Regression tests for agent response parsing and normalization."""

from __future__ import annotations

import pytest

from orchestrator.parse import (
    AgentOutputParseError,
    extract_fenced_block,
    normalize_structured_output,
    parse_agent_json_or_yaml,
)
from orchestrator.schema_validation import build_model, validate_payload


PADDED_EVALUATOR_OUTPUT = """All values match the prompt data. The evaluation is confirmed. Here's the result:

```json
{
  "passed": true,
  "trigger": "volume_shock",
  "confidence_multiplier": 1.0,
  "details": "volume_shock: current=2410719.43 > baseline_median=291903.31 × 3.0",
  "error": null
}
```

**Summary:** The market passed the **volume_shock** filter."""


def test_parse_padded_evaluator_output_with_fenced_json():
    parsed = parse_agent_json_or_yaml(PADDED_EVALUATOR_OUTPUT)
    assert parsed["passed"] is True
    assert parsed["trigger"] == "volume_shock"
    assert parsed["confidence_multiplier"] == 1.0
    assert "market_id" not in parsed


def test_parse_raw_json_object():
    raw = (
        '{"market_id": "0xabc", "passed": false, "trigger": null, '
        '"confidence_multiplier": 1.0, "details": "ok", "error": null}'
    )
    parsed = parse_agent_json_or_yaml(raw)
    assert parsed["market_id"] == "0xabc"
    assert parsed["passed"] is False


def test_extract_fenced_block_from_prose():
    body = extract_fenced_block(PADDED_EVALUATOR_OUTPUT)
    assert body is not None
    assert body.startswith("{")
    assert '"passed": true' in body


def test_normalize_structured_output_backfills_market_id():
    payload = {"market_id": "0x02deb9538f5c123373adaa4ee6217b01745f1662bc902e46ac92f3fe6f8741e8"}
    parsed = {
        "passed": True,
        "trigger": "volume_shock",
        "confidence_multiplier": 1.0,
        "details": "x",
        "error": None,
    }
    out = normalize_structured_output(
        "evaluator",
        payload,
        parsed,
        output_schema={"market_id": "string", "passed": "boolean"},
    )
    assert out["market_id"] == payload["market_id"]


def test_normalize_structured_output_skips_when_market_id_present():
    payload = {"market_id": "0xabc"}
    parsed = {"market_id": "0xdef", "passed": False}
    out = normalize_structured_output(
        "evaluator",
        payload,
        parsed,
        output_schema={"market_id": "string"},
    )
    assert out["market_id"] == "0xdef"


def test_padded_evaluator_output_passes_schema_after_normalization():
    parsed = parse_agent_json_or_yaml(PADDED_EVALUATOR_OUTPUT)
    market_id = "0x02deb9538f5c123373adaa4ee6217b01745f1662bc902e46ac92f3fe6f8741e8"
    normalized = normalize_structured_output(
        "evaluator",
        {"market_id": market_id},
        parsed,
        output_schema={
            "market_id": "string",
            "passed": "boolean",
            "trigger": "string | null",
            "confidence_multiplier": "float",
            "details": "string",
            "error": "string | null",
        },
    )
    model = build_model("EvaluatorOutput", {
        "market_id": "string",
        "passed": "boolean",
        "trigger": "string | null",
        "confidence_multiplier": "float",
        "details": "string",
        "error": "string | null",
    })
    validate_payload("evaluator", "output", model, normalized)


def test_parse_empty_output_raises():
    with pytest.raises(AgentOutputParseError, match="empty"):
        parse_agent_json_or_yaml("   ")
