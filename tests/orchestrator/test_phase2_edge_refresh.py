"""Phase 2: skip markets with an open bet; edge-refresh handoff via pending_edge_refresh."""

from __future__ import annotations

from typing import Any

import pytest

from config.trading_constants import PENDING_EDGE_REFRESH_KEY
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
    phases.phase2_quantitative_routing(vault, [market], runner=runner)

    assert calls == []


def test_phase2_edge_refresh_sets_pending_flag_not_deep_researcher(monkeypatch, vault):
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
    roles: list[str] = []

    def fake_runner(role: str, payload: dict[str, Any]) -> dict[str, Any]:
        roles.append(role)
        assert role == "re_evaluator"
        assert payload["review_kind"] == "edge_research_refresh"
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
    phases.phase2_quantitative_routing(vault, [market], runner=fake_runner)

    assert roles == ["re_evaluator"]
    assert "deep_researcher" not in roles
    filt = vault.read_filter_log(market_id)
    assert filt is not None
    assert filt.get(PENDING_EDGE_REFRESH_KEY) is True


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
    phases.phase2_quantitative_routing(vault, [market], runner=boom)


def test_phase3_ignores_edge_dq_without_pending_flag(monkeypatch, vault):
    """Phase 3 must not poll trade logs for edge refresh (no pending flag)."""
    market_id = "m-no-pending"
    vault.write_filter_log(
        market_id,
        {
            "market_id": market_id,
            "passed": True,
            "trigger": "x",
            "confidence_multiplier": 1.0,
            "details": "ok",
            "error": None,
        },
    )
    vault.write_research_report(
        market_id,
        {"market_id": market_id, "estimated_p": 0.5, "error": None},
        "## Bull Thesis\n\nx\n\n## Bear Thesis\n\ny\n\n## Post-Mortem\n",
    )
    vault.write_trade_log(
        market_id,
        {
            "market_id": market_id,
            "allocation_usd": 0.0,
            "below_edge_threshold": True,
            "executed": False,
            "transaction_hash": None,
            "error": None,
        },
    )

    queue = phases.build_phase3_queue(vault)
    assert queue == []


def test_phase3_processes_pending_edge_refresh(monkeypatch, vault):
    market_id = "m-pending"
    vault.write_filter_log(
        market_id,
        {
            "market_id": market_id,
            "passed": True,
            "trigger": "x",
            "confidence_multiplier": 1.0,
            "details": "ok",
            "error": None,
            PENDING_EDGE_REFRESH_KEY: True,
        },
    )
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

    monkeypatch.setattr(
        scraper,
        "fetch_market_row",
        lambda mid: MarketRow(market_id=mid, market_title="T", market_data={}),
    )

    def runner(role: str, payload: dict[str, Any]) -> Any:
        if role == "deep_researcher":
            return (
                "---\n"
                f'market_id: "{payload["market_id"]}"\n'
                "estimated_p: 0.6\n"
                "error: null\n"
                "---\n\n"
                "## Bull Thesis\n\nb\n\n## Bear Thesis\n\nb\n\n## Post-Mortem\n"
            )
        raise AssertionError(f"unexpected role {role}")

    out = phases.phase3_qualitative_pipeline(vault, runner=runner)
    assert len(out) == 1
    filt = vault.read_filter_log(market_id)
    assert filt is not None
    assert not filt.get(PENDING_EDGE_REFRESH_KEY)
    text = vault.read_active_research(market_id)
    assert text
    fm, _ = split_yaml_frontmatter_markdown(text)
    assert int(fm.get("edge_research_refresh_count") or 0) == 2
