"""F5 regression: a Deep Researcher market_id mismatch flags the market inactive."""

from __future__ import annotations

from typing import Any

import pytest

from config.trading_constants import STATUS_INACTIVE, STATUS_KEY
from obsidian_utils import ObsidianManager
from orchestrator import phases, scraper
from orchestrator.runner import _stub_deep_researcher_markdown
from orchestrator.scraper import MarketRow
from tests.orchestrator.test_phase3_helpers import briefer_ok, deep_researcher_complete


@pytest.fixture()
def vault(tmp_path):
    return ObsidianManager(vault_base=tmp_path)


def test_mismatched_market_id_flags_inactive(monkeypatch, vault):
    vault.write_filter_log(
        "0xRIGHT",
        {
            "market_id": "0xRIGHT",
            "passed": True,
            "trigger": "stub",
            "confidence_multiplier": 1.0,
            "details": "ok",
            "error": None,
        },
    )
    monkeypatch.setattr(
        scraper,
        "fetch_market_row",
        lambda mid: MarketRow(market_id=mid, market_title="T", market_data={}),
    )

    def runner(role: str, payload: dict[str, Any]) -> Any:
        if role == "briefer":
            return briefer_ok(payload)
        if role == "deep_researcher":
            return {
                "status": "complete",
                "market_id": "0xWRONG",
                "estimated_p": 0.6,
                "markdown": _stub_deep_researcher_markdown("0xWRONG", estimated_p=0.6),
            }
        raise AssertionError(role)

    monkeypatch.setattr(
        phases,
        "fetch_research_bundle",
        lambda queries: [
            {"query": q, "research_data": "x", "error": None} for q in queries
        ],
    )

    out = phases.phase3_qualitative_pipeline(vault, runner=runner)
    assert out == []
    active = vault.read_market_record("0xRIGHT", "active")
    filt = vault.read_filter_log("0xRIGHT")
    flagged = (active and active.get(STATUS_KEY) == STATUS_INACTIVE) or (
        filt and filt.get(STATUS_KEY) == STATUS_INACTIVE
    )
    assert flagged


def test_matching_market_id_proceeds(monkeypatch, vault):
    vault.write_filter_log(
        "0xRIGHT",
        {
            "market_id": "0xRIGHT",
            "passed": True,
            "trigger": "stub",
            "confidence_multiplier": 1.0,
            "details": "ok",
            "error": None,
        },
    )
    monkeypatch.setattr(
        scraper,
        "fetch_market_row",
        lambda mid: MarketRow(market_id=mid, market_title="T", market_data={}),
    )

    def runner(role: str, payload: dict[str, Any]) -> Any:
        if role == "briefer":
            return briefer_ok(payload)
        if role == "deep_researcher":
            return deep_researcher_complete(payload, estimated_p=0.6)
        raise AssertionError(role)

    monkeypatch.setattr(
        phases,
        "fetch_research_bundle",
        lambda queries: [
            {"query": q, "research_data": "x", "error": None} for q in queries
        ],
    )

    out = phases.phase3_qualitative_pipeline(vault, runner=runner)
    assert len(out) == 1
