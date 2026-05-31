"""Phase 2: skip markets with an open bet; edge-disqualification research refresh."""

from __future__ import annotations

from typing import Any

import pytest

from obsidian_utils import ObsidianManager
from orchestrator import phases, scraper
from orchestrator.research import split_yaml_frontmatter_markdown
from orchestrator.scraper import MarketRow


@pytest.fixture()
def vault(tmp_path):
    return ObsidianManager(vault_base=tmp_path)


def test_phase2_skips_when_open_trade_shows_bet(monkeypatch, vault):
    market_id = "m-bet"
    vault.write_trade_log(
        market_id,
        {
            "market_id": market_id,
            "allocation_usd": 50.0,
            "score": 0.2,
            "below_edge_threshold": False,
            "executed": True,
            "transaction_hash": "0xabc",
            "error": None,
        },
    )
    calls: list[str] = []

    def runner(role: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(role)
        return {"error": "should not run"}

    monkeypatch.setattr(scraper, "get_market_trends", lambda mid, limit: [])
    monkeypatch.setattr(scraper, "trends_limit_for_filters", lambda: 10)

    market = MarketRow(market_id=market_id, market_title="T", market_data={})
    passed, refresh = phases.phase2_quantitative_routing(vault, [market], runner=runner)

    assert calls == []
    assert passed == []
    assert refresh == []


def test_phase2_edge_refresh_enqueues_refresh_row(monkeypatch, vault):
    market_id = "m-edge"
    vault.write_filter_log(
        market_id,
        {
            "market_id": market_id,
            "passed": True,
            "trigger": "breakout",
            "confidence_multiplier": 1.2,
            "details": "prior",
            "error": None,
        },
    )
    vault.write_research_report(
        market_id,
        {"market_id": market_id, "estimated_p": 0.55, "error": None},
        "## Bull Thesis\n\nx\n\n## Bear Thesis\n\ny\n\n## Post-Mortem\n",
    )
    vault.write_trade_log(
        market_id,
        {
            "market_id": market_id,
            "allocation_usd": 0.0,
            "score": -0.01,
            "below_edge_threshold": True,
            "executed": False,
            "transaction_hash": None,
            "error": None,
        },
    )

    monkeypatch.setattr(scraper, "get_market_trends", lambda mid, limit: [])
    monkeypatch.setattr(scraper, "trends_limit_for_filters", lambda: 10)

    def fake_runner(role: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert role == "re_evaluator"
        assert payload["review_kind"] == "edge_research_refresh"
        assert payload["trade_log"]["below_edge_threshold"] is True
        return {
            "market_id": payload["market_id"],
            "passed": True,
            "trigger": "volume_shock",
            "confidence_multiplier": 1.1,
            "details": "regime changed",
            "error": None,
            "retry_deep_research": True,
            "refresh_reason": "quantitative_regime_changed",
        }

    market = MarketRow(market_id=market_id, market_title="T", market_data={})
    passed, refresh = phases.phase2_quantitative_routing(vault, [market], runner=fake_runner)

    assert passed == []
    assert len(refresh) == 1
    assert refresh[0]["_edge_research_refresh"] is True
    assert refresh[0]["evaluator_output"]["retry_deep_research"] is True


def test_phase2_edge_refresh_respects_cap(monkeypatch, vault):
    market_id = "m-cap"
    vault.write_filter_log(
        market_id,
        {
            "market_id": market_id,
            "passed": True,
            "trigger": "breakout",
            "confidence_multiplier": 1.0,
            "details": "prior",
            "error": None,
        },
    )
    vault.write_research_report(
        market_id,
        {
            "market_id": market_id,
            "estimated_p": 0.5,
            "error": None,
            "edge_research_refresh_count": 3,
        },
        "## Bull Thesis\n\nx\n\n## Bear Thesis\n\ny\n\n## Post-Mortem\n",
    )
    vault.write_trade_log(
        market_id,
        {
            "market_id": market_id,
            "allocation_usd": 0.0,
            "score": 0.0,
            "below_edge_threshold": True,
            "executed": False,
            "transaction_hash": None,
            "error": None,
        },
    )
    monkeypatch.setattr(scraper, "get_market_trends", lambda mid, limit: [])
    monkeypatch.setattr(scraper, "trends_limit_for_filters", lambda: 10)

    def boom(_role: str, _payload: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("re_evaluator should not run when cap reached")

    market = MarketRow(market_id=market_id, market_title="T", market_data={})
    passed, refresh = phases.phase2_quantitative_routing(vault, [market], runner=boom)

    assert passed == []
    assert refresh == []


def test_phase3_preserves_edge_research_refresh_count_on_normal_rerun(vault):
    """Normal quantitative re-research must not reset the cumulative edge-refresh counter."""
    market_id = "m-preserve"
    vault.write_research_report(
        market_id,
        {
            "market_id": market_id,
            "estimated_p": 0.5,
            "error": None,
            "edge_research_refresh_count": 2,
        },
        "## Bull Thesis\n\nx\n\n## Bear Thesis\n\ny\n\n## Post-Mortem\n",
    )

    def runner(role: str, payload: dict[str, Any]) -> dict[str, Any] | str:
        if role == "briefer":
            return {"market_id": payload["market_id"], "summary": "ok", "error": None}
        if role == "deep_researcher":
            return (
                "---\n"
                f'market_id: "{payload["market_id"]}"\n'
                "estimated_p: 0.55\n"
                "error: null\n"
                "---\n\n"
                "## Bull Thesis\n\nb\n\n## Bear Thesis\n\nb\n\n## Post-Mortem\n"
            )
        raise AssertionError(role)

    row = {
        "market_id": market_id,
        "market_title": "T",
        "market_description": "",
        "market_data": {},
    }
    out = phases.phase3_qualitative_pipeline(vault, [row], runner=runner)
    assert len(out) == 1
    text = vault.read_active_research(market_id)
    assert text
    fm, _ = split_yaml_frontmatter_markdown(text)
    assert int(fm.get("edge_research_refresh_count") or 0) == 2


def test_phase3_increments_edge_research_refresh_count_on_edge_refresh(vault):
    market_id = "m-inc"
    vault.write_research_report(
        market_id,
        {
            "market_id": market_id,
            "estimated_p": 0.5,
            "error": None,
            "edge_research_refresh_count": 1,
        },
        "## Bull Thesis\n\nx\n\n## Bear Thesis\n\ny\n\n## Post-Mortem\n",
    )

    def runner(role: str, payload: dict[str, Any]) -> dict[str, Any] | str:
        if role == "briefer":
            return {"market_id": payload["market_id"], "summary": "ok", "error": None}
        if role == "deep_researcher":
            return (
                "---\n"
                f'market_id: "{payload["market_id"]}"\n'
                "estimated_p: 0.6\n"
                "error: null\n"
                "---\n\n"
                "## Bull Thesis\n\nb\n\n## Bear Thesis\n\nb\n\n## Post-Mortem\n"
            )
        raise AssertionError(role)

    row = {
        "market_id": market_id,
        "market_title": "T",
        "market_description": "",
        "market_data": {},
        "_edge_research_refresh": True,
    }
    out = phases.phase3_qualitative_pipeline(vault, [row], runner=runner)
    assert len(out) == 1
    text = vault.read_active_research(market_id)
    assert text
    fm, _ = split_yaml_frontmatter_markdown(text)
    assert int(fm.get("edge_research_refresh_count") or 0) == 2


def test_merge_phase3_primary_wins_on_duplicate_id():
    primary = [
        {
            "market_id": "a",
            "evaluator_output": {"confidence_multiplier": 2.0},
        }
    ]
    refresh = [
        {
            "market_id": "a",
            "evaluator_output": {"confidence_multiplier": 9.0},
        }
    ]
    out = phases.merge_phase3_inputs(primary, refresh, 10)
    assert len(out) == 1
    assert out[0]["evaluator_output"]["confidence_multiplier"] == 2.0
