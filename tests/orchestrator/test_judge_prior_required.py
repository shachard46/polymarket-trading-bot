"""Judge mode: a response that drops every prior field is re-asked once (Sonnet omits them on large bundles)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from orchestrator.phases.agent_call import invoke_deep_researcher

_BASE = {
    "status": "complete", "market_id": "0xm", "estimated_p": 0.6, "p_low": 0.5, "p_high": 0.7, "confidence": "medium",
    "resolution_checklist": ["r"], "key_assumptions": ["a"], "as_of": "2026-11-22", "markdown": "## Bull Thesis\n- x",
}
_PRIOR = {"prior": 0.55, "prior_kind": "judgment", "base_rate": None, "crux_verdicts": [], "discounted_sources": []}


def _runner(replies: list[dict[str, Any]], seen: list[dict[str, Any]]):
    def run(role: str, payload: dict[str, Any]) -> str:
        seen.append(payload)
        return json.dumps(replies[min(len(seen), len(replies)) - 1])
    return run


def _dr_in(mode: str) -> dict[str, Any]:
    return {"market_id": "0xm", "mode": mode, "title": "t", "description": "d", "today": "2026-11-22", "research_bundle": []}


def test_judge_missing_prior_fields_is_retried_with_error_text():
    seen: list[dict[str, Any]] = []
    out = invoke_deep_researcher(_runner([_BASE, {**_BASE, **_PRIOR}], seen), _dr_in("judge"))
    assert len(seen) == 2
    assert "prior_kind" in seen[1]["format_validation_error"]
    assert out.prior_kind == "judgment" and out.prior == 0.55


def test_judge_missing_prior_fields_after_retries_raises():
    seen: list[dict[str, Any]] = []
    with pytest.raises(ValidationError):
        invoke_deep_researcher(_runner([_BASE], seen), _dr_in("judge"))
    assert len(seen) > 1


def test_judge_with_prior_fields_is_not_retried():
    seen: list[dict[str, Any]] = []
    invoke_deep_researcher(_runner([{**_BASE, **_PRIOR}], seen), _dr_in("judge"))
    assert len(seen) == 1


def test_non_judge_modes_do_not_require_prior_fields():
    seen: list[dict[str, Any]] = []
    out = invoke_deep_researcher(_runner([_BASE], seen), _dr_in("quick"))
    assert len(seen) == 1 and out.prior_kind is None
