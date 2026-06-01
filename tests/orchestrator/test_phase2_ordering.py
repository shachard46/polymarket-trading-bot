"""Phase 2 passes filter_directives to the evaluator (not raw history)."""

from __future__ import annotations

from typing import Any

import pytest

from config.trading_constants import ERROR_LOG_KEY, FILTERS, STATUS_INACTIVE, STATUS_KEY
from orchestrator import phases
from orchestrator.scraper import MarketRow
from orchestrator.state import is_inactive


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    from obsidian_utils import ObsidianManager

    v = ObsidianManager(vault_base=tmp_path)
    v.cold_start_protocol()
    return v


def test_phase2_passes_filter_directives_to_evaluator(vault):
    captured: dict[str, Any] = {}

    def fake_runner(role: str, payload: dict[str, Any]) -> dict[str, Any]:
        captured["role"] = role
        captured["payload"] = payload
        return {
            "market_id": payload["market_id"],
            "passed": False,
            "trigger": None,
            "confidence_multiplier": 1.0,
            "details": "test",
            "error": None,
        }

    market = MarketRow(
        market_id="m1", market_title="Test market", market_data={}
    )
    phases.phase2_quantitative_routing(vault, [market], runner=fake_runner)

    assert captured["role"] == "evaluator"
    assert "historic_market_data" not in captured["payload"]
    assert "filter_directives" in captured["payload"]
    assert captured["payload"]["filter_directives"]["breakout_pct_shift"] == FILTERS[
        "breakout_pct_shift"
    ]

    record = vault.read_filter_log("m1")
    assert record is not None
    assert record["passed"] is False
    assert is_inactive(record)
    assert record[STATUS_KEY] == STATUS_INACTIVE
    assert record[ERROR_LOG_KEY]["details"] == "test"
    assert record[ERROR_LOG_KEY]["trigger"] is None
