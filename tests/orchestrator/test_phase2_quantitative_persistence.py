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


def test_phase2_second_tick_skips_inactive_filter(vault):
    market_id = "m-ghost"
    calls: list[str] = []

    def runner(role: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(role)
        return _evaluator_fail(payload["market_id"])

    market = MarketRow(market_id=market_id, market_title="T", market_data={})
    phases.phase2_quantitative_routing(vault, [market], runner=runner)
    assert len(calls) == 1
    assert vault.is_market_inactive(market_id, dir_key="filters")

    phases.phase2_quantitative_routing(vault, [market], runner=runner)
    assert len(calls) == 1


def test_phase2_replay_allows_reprocessing(vault):
    market_id = "m-replay"
    calls: list[str] = []

    def runner(role: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(role)
        return _evaluator_fail(payload["market_id"])

    market = MarketRow(market_id=market_id, market_title="T", market_data={})
    phases.phase2_quantitative_routing(vault, [market], runner=runner)
    assert len(calls) == 1

    summary = replay_inactive(vault, market_ids=[market_id], dir_keys=("filters",))
    assert summary["cleared"] >= 1
    record = vault.read_filter_log(market_id)
    assert record is not None
    assert not is_inactive(record)
    assert STATUS_KEY not in record or record.get(STATUS_KEY) != STATUS_INACTIVE

    phases.phase2_quantitative_routing(vault, [market], runner=runner)
    assert len(calls) == 2
