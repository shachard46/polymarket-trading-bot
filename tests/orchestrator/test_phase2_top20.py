"""Phase 2 caps the qualitative queue at OPENCLAW_TOP_MARKETS (default 20)."""

from __future__ import annotations

from typing import Any

from obsidian_utils import ObsidianManager
from orchestrator import phases, scraper
from orchestrator.scraper import MarketRow


def test_phase2_sorts_by_confidence_multiplier_and_caps(monkeypatch, tmp_path):
    vault = ObsidianManager(vault_base=tmp_path)
    monkeypatch.setattr(scraper, "get_market_trends", lambda mid, limit: [])
    monkeypatch.setattr(scraper, "trends_limit_for_filters", lambda: 10)
    monkeypatch.setattr(phases, "top_qualitative_markets", lambda: 2)

    mult_by_id = {"low": 1.0, "high": 5.0, "mid": 3.0}

    def fake_runner(role: str, payload: dict[str, Any]) -> dict[str, Any]:
        mid = payload["market_id"]
        assert role == "evaluator"
        return {
            "market_id": mid,
            "passed": True,
            "trigger": "stub",
            "confidence_multiplier": mult_by_id[mid],
            "details": "ok",
            "error": None,
        }

    markets = [
        MarketRow(market_id="low", market_title="L", market_data={}),
        MarketRow(market_id="high", market_title="H", market_data={}),
        MarketRow(market_id="mid", market_title="M", market_data={}),
    ]
    out = phases.phase2_quantitative_routing(vault, markets, runner=fake_runner)

    assert [r["market_id"] for r in out] == ["high", "mid"]
