"""F1 regression: evaluator must receive oldest-first market history."""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator import phases, scraper
from orchestrator.scraper import MarketRow


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    from obsidian_utils import ObsidianManager

    return ObsidianManager(vault_base=tmp_path)


def test_phase2_passes_oldest_first_to_evaluator(monkeypatch, vault):
    series_oldest_first = [
        {"datetime": "2026-01-01T00:00:00Z", "yes_price": 0.5},
        {"datetime": "2026-01-02T00:00:00Z", "yes_price": 0.6},
        {"datetime": "2026-01-03T00:00:00Z", "yes_price": 0.7},
    ]
    monkeypatch.setattr(
        scraper, "get_market_trends", lambda mid, limit: list(series_oldest_first)
    )
    monkeypatch.setattr(scraper, "trends_limit_for_filters", lambda: 200)

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

    historic = captured["payload"]["historic_market_data"]
    assert historic[0]["datetime"] <= historic[-1]["datetime"]
    assert historic == series_oldest_first
