"""Hub merge of signal_bundle onto slim quantitative agent output."""

from __future__ import annotations

from typing import Any

from config.trading_constants import FILTERS
from orchestrator.evaluator_output import attach_signal_bundle, set_fetch_signal_bundle_impl
from orchestrator.phases import _run_structured_agent
from orchestrator.runner import spawn_agent


_RE_EVALUATOR_IN = {
    "market_id": "0xabc",
    "review_kind": "quantitative",
    "filter_directives": {"breakout_pct_shift": 0.1},
    "historic_signal_bundle": None,
    "prior_filter_trigger": None,
    "prior_evaluator_details": None,
    "prior_filter_log": None,
    "research_markdown": None,
    "trade_log": None,
}


def test_attach_signal_bundle_merges_for_quantitative_roles_only():
    bundle = {"market_id": "0xabc", "signals": {"volume_shock": {"ratio": 2.0}}}

    def fake_fetch(mid: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        assert mid == "0xabc"
        assert overrides == {"breakout_pct_shift": 0.1}
        return bundle

    set_fetch_signal_bundle_impl(fake_fetch)
    try:
        slim = {
            "market_id": "0xabc",
            "passed": True,
            "trigger": "volume_shock",
            "confidence_multiplier": 1.2,
            "details": "ok",
            "error": None,
        }
        payload = {"market_id": "0xabc", "filter_directives": {"breakout_pct_shift": 0.1}}
        merged_ev = attach_signal_bundle("evaluator", payload, slim)
        assert merged_ev["signal_bundle"] == bundle
        assert merged_ev["passed"] is True

        slim_re = {**slim, "retry_deep_research": False, "refresh_reason": None}
        merged_re = attach_signal_bundle("re_evaluator", {**_RE_EVALUATOR_IN, **payload}, slim_re)
        assert merged_re["signal_bundle"] == bundle

        unchanged = attach_signal_bundle("briefer", {"market_id": "0xabc"}, slim)
        assert "signal_bundle" not in unchanged
    finally:
        set_fetch_signal_bundle_impl(None)


def test_run_structured_agent_attaches_bundle_after_stub_spawn():
    def runner(role: str, payload: dict[str, Any]) -> dict[str, Any]:
        return spawn_agent(role, payload)

    parsed, err = _run_structured_agent(
        runner,
        "evaluator",
        {"market_id": "0xmerge", "filter_directives": dict(FILTERS)},
    )
    assert err is None
    assert parsed is not None
    assert parsed["market_id"] == "0xmerge"
    assert parsed["signal_bundle"]["market_id"] == "0xmerge"
    assert parsed["signal_bundle"].get("stub") is True


def test_spawn_agent_returns_slim_evaluator_without_signal_bundle():
    out = spawn_agent(
        "evaluator",
        {"market_id": "0xslim", "filter_directives": dict(FILTERS)},
    )
    assert isinstance(out, dict)
    assert "signal_bundle" not in out
    assert out["error"] is None


def test_run_structured_agent_attaches_bundle_for_re_evaluator():
    def runner(role: str, payload: dict[str, Any]) -> dict[str, Any]:
        return spawn_agent(role, payload)

    payload = {
        "market_id": "0xremerge",
        "review_kind": "quantitative",
        "filter_directives": dict(FILTERS),
        "historic_signal_bundle": None,
        "prior_filter_trigger": None,
        "prior_evaluator_details": None,
        "prior_filter_log": None,
        "research_markdown": None,
        "trade_log": None,
    }
    parsed, err = _run_structured_agent(runner, "re_evaluator", payload)
    assert err is None
    assert parsed is not None
    assert parsed["signal_bundle"]["market_id"] == "0xremerge"
    assert parsed["retry_deep_research"] is False


def test_spawn_agent_returns_slim_re_evaluator_without_signal_bundle():
    out = spawn_agent(
        "re_evaluator",
        {
            "market_id": "0xreslim",
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
    assert isinstance(out, dict)
    assert "signal_bundle" not in out
    assert out["error"] is None
