"""Phase 2: persist soft quantitative failures and skip until replay."""

from __future__ import annotations

from typing import Any

import pytest

from config.trading_constants import STATUS_INACTIVE, STATUS_KEY
from obsidian_utils import ObsidianManager
from orchestrator import phases
from orchestrator.scraper import MarketRow
from orchestrator.state import is_inactive, replay_inactive


@pytest.fixture()
def vault(tmp_path):
    v = ObsidianManager(vault_base=tmp_path)
    v.cold_start_protocol()
    return v


def _evaluator_fail(market_id: str) -> dict[str, Any]:
    return {
        "market_id": market_id,
        "passed": False,
        "trigger": "breakout_pct_shift",
        "confidence_multiplier": 0.5,
        "details": "below threshold",
        "error": None,
    }


def test_phase2_soft_fail_goes_to_re_evaluator_next_tick(vault):
    """passed:false is not inactive; the next tick re-checks it and it can pass."""
    market_id = "m-ghost"
    calls: list[tuple[str, dict[str, Any]]] = []

    def runner(role: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((role, payload))
        if role == "re_evaluator":
            return {
                **_evaluator_fail(payload["market_id"]),
                "passed": True,
                "confidence_multiplier": 1.2,
                "retry_deep_research": False,
                "refresh_reason": None,
            }
        return _evaluator_fail(payload["market_id"])

    market = MarketRow(market_id=market_id, market_title="T", market_data={})
    phases.phase2_quantitative_routing(vault, [market], runner=runner)
    assert [r for r, _ in calls] == ["evaluator"]
    assert not vault.is_market_inactive(market_id, dir_key="filters")

    phases.phase2_quantitative_routing(vault, [market], runner=runner)
    role, payload = calls[1]
    assert role == "re_evaluator"
    assert payload["review_kind"] == "quantitative"
    prior = payload["prior_filter_result"]
    assert set(prior) == {
        "passed", "trigger", "confidence_multiplier", "details", "signal_bundle",
    }
    assert prior["passed"] is False and prior["trigger"] == "breakout_pct_shift"
    assert vault.read_filter_log(market_id)["passed"] is True


def test_phase2_soft_fail_leaves_nothing_to_replay(vault):
    market_id = "m-replay"

    def runner(role: str, payload: dict[str, Any]) -> dict[str, Any]:
        return _evaluator_fail(payload["market_id"])

    market = MarketRow(market_id=market_id, market_title="T", market_data={})
    phases.phase2_quantitative_routing(vault, [market], runner=runner)
    summary = replay_inactive(vault, market_ids=[market_id], dir_keys=("filters",))
    assert summary["cleared"] == 0


def test_phase2_error_still_flags_inactive_and_is_skipped(vault):
    market_id = "m-err"
    calls: list[str] = []

    def runner(role: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(role)
        return {**_evaluator_fail(payload["market_id"]), "error": "tool exploded"}

    market = MarketRow(market_id=market_id, market_title="T", market_data={})
    phases.phase2_quantitative_routing(vault, [market], runner=runner)
    assert vault.is_market_inactive(market_id, dir_key="filters")
    phases.phase2_quantitative_routing(vault, [market], runner=runner)
    assert len(calls) == 1
    summary = replay_inactive(vault, market_ids=[market_id], dir_keys=("filters",))
    assert summary["cleared"] == 1
