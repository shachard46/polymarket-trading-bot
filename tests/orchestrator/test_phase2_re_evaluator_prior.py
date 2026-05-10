"""Re-Evaluator receives prior filter fields from the vault."""

from __future__ import annotations

from typing import Any

from obsidian_utils import ObsidianManager
from orchestrator import phases, scraper
from orchestrator.scraper import MarketRow


def test_phase2_passes_prior_filter_context_to_re_evaluator(monkeypatch, tmp_path):
    vault = ObsidianManager(vault_base=tmp_path)
    market_id = "cond-1"
    vault.write_filter_log(
        market_id,
        {
            "market_id": market_id,
            "passed": True,
            "trigger": "volume_shock",
            "confidence_multiplier": 1.5,
            "details": "earlier pass",
            "error": None,
        },
    )
    vault.write_research_report(
        market_id,
        {"market_id": market_id, "estimated_p": 0.42, "error": None},
        "## Bull Thesis\n\nx\n\n## Bear Thesis\n\ny\n\n## Post-Mortem\n",
    )

    monkeypatch.setattr(scraper, "get_market_trends", lambda mid, limit: [])
    monkeypatch.setattr(scraper, "trends_limit_for_filters", lambda: 10)

    captured: dict[str, Any] = {}

    def fake_runner(role: str, payload: dict[str, Any]) -> dict[str, Any]:
        captured["role"] = role
        captured["payload"] = payload
        return {
            "market_id": payload["market_id"],
            "passed": True,
            "trigger": "breakout",
            "confidence_multiplier": 1.0,
            "details": "ok",
            "error": None,
            "retry_deep_research": False,
            "refresh_reason": None,
        }

    market = MarketRow(market_id=market_id, market_title="T", market_data={})
    passed, refresh = phases.phase2_quantitative_routing(vault, [market], runner=fake_runner)

    assert captured["role"] == "re_evaluator"
    assert captured["payload"]["review_kind"] == "quantitative"
    assert captured["payload"]["prior_filter_trigger"] == "volume_shock"
    assert captured["payload"]["prior_evaluator_details"] == "earlier pass"
    assert captured["payload"]["prior_filter_log"] is None
    assert passed and refresh == []
