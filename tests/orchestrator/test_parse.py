"""Regression tests for agent response parsing and normalization."""

from __future__ import annotations

import json

import pytest

from orchestrator.parse import (
    AgentOutputParseError,
    coerce_deep_researcher_state_machine,
    extract_fenced_block,
    normalize_structured_output,
    parse_agent_json_or_yaml,
)
from orchestrator.research import parse_deep_researcher
from orchestrator.runner import _stub_deep_researcher_markdown
from orchestrator.schema_validation import build_model, validate_payload

PADDED_TRIAGE_OUTPUT = """All values match the prompt data. The evaluation is confirmed. Here's the result:

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


def test_parse_padded_triage_output_with_fenced_json():
    parsed = parse_agent_json_or_yaml(PADDED_TRIAGE_OUTPUT)
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
    body = extract_fenced_block(PADDED_TRIAGE_OUTPUT)
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
        "triage",
        payload,
        parsed,
        output_schema={"market_id": "string", "passed": "boolean"},
    )
    assert out["market_id"] == payload["market_id"]


def test_normalize_structured_output_skips_when_market_id_present():
    payload = {"market_id": "0xabc"}
    parsed = {"market_id": "0xdef", "passed": False}
    out = normalize_structured_output(
        "triage",
        payload,
        parsed,
        output_schema={"market_id": "string"},
    )
    assert out["market_id"] == "0xdef"


def test_padded_triage_output_passes_schema_after_normalization():
    parsed = parse_agent_json_or_yaml(PADDED_TRIAGE_OUTPUT)
    market_id = "0x02deb9538f5c123373adaa4ee6217b01745f1662bc902e46ac92f3fe6f8741e8"
    normalized = normalize_structured_output(
        "triage",
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
    model = build_model("TriageOutput", {
        "market_id": "string",
        "passed": "boolean",
        "trigger": "string | null",
        "confidence_multiplier": "float",
        "details": "string",
        "error": "string | null",
    })
    validate_payload("triage", "output", model, normalized)


def test_parse_empty_output_raises():
    with pytest.raises(AgentOutputParseError, match="empty"):
        parse_agent_json_or_yaml("   ")


def test_parse_double_encoded_json_string():
    """OpenClaw sometimes returns the JSON object as a JSON-encoded string."""
    inner = (
        '{"passed": false, "trigger": null, "confidence_multiplier": 1.0, '
        '"details": "No filter triggered. Notes: all filters evaluated", '
        '"error": null, '
        '"market_id": "0x3a3e8edb0923b69720fd41c274191f830a6ca1f7da9b6484fd82e1b4d8c3d1ec"}'
    )
    parsed = parse_agent_json_or_yaml(json.dumps(inner))
    assert parsed["passed"] is False
    assert parsed["market_id"].startswith("0x")


def test_coerce_deep_researcher_state_machine_needs_more_unchanged():
    raw = {"status": "needs_more_data", "new_queries": ["follow-up"]}
    assert coerce_deep_researcher_state_machine(raw, {"market_id": "x"}) == raw


def test_coerce_deep_researcher_state_machine_rejects_markdown_and_missing_status():
    for raw in (
        _stub_deep_researcher_markdown("0xabc", estimated_p=0.42),
        {"markdown": _stub_deep_researcher_markdown("0xabc", estimated_p=0.42)},
        {"estimated_p": 0.4},
    ):
        with pytest.raises(AgentOutputParseError):
            coerce_deep_researcher_state_machine(raw, {"market_id": "0xabc"})


def test_normalize_structured_output_rejects_legacy_briefer_summary():
    with pytest.raises(ValueError, match="research_queries"):
        normalize_structured_output(
            "briefer",
            {"market_id": "0xabc"},
            {"market_id": "0xabc", "summary": "old style context"},
            output_schema={"market_id": "string"},
        )


def _research_md(body_suffix: str = "") -> str:
    return (
        "---\n"
        'market_id: "x"\n'
        "estimated_p: 0.5\n"
        "error: null\n"
        "---\n\n"
        "## Bull Thesis\n\nb.\n\n"
        "## Bear Thesis\n\nbe.\n\n"
        "## Post-Mortem\n"
        f"{body_suffix}"
    )


def test_parse_accepts_empty_post_mortem_section():
    r = parse_deep_researcher(_research_md())
    assert "b." in r.body
    assert "## Post-Mortem" in r.body


def test_parse_rejects_nonempty_post_mortem_section():
    with pytest.raises(ValueError, match="Post-Mortem"):
        parse_deep_researcher(_research_md("oops"))
