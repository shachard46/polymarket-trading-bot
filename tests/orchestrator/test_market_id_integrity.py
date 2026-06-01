"""F5 regression: a Deep Researcher market_id mismatch flags the market inactive."""

from __future__ import annotations

from typing import Any

import pytest

from config.trading_constants import STATUS_INACTIVE, STATUS_KEY
from obsidian_utils import ObsidianManager
from orchestrator import phases, scraper
from orchestrator.scraper import MarketRow


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
            return {
                "market_id": payload["market_id"],
                "summary": "ok",
                "error": None,
            }
        if role == "deep_researcher":
            return (
                "---\n"
                'market_id: "0xWRONG"\n'
                "estimated_p: 0.6\n"
                "error: null\n"
                "---\n\n"
                "## Bull Thesis\n\nbody\n\n## Bear Thesis\n\nbody\n\n## Post-Mortem\n"
            )
        raise AssertionError(role)

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
            return {"market_id": payload["market_id"], "summary": "ok", "error": None}
        if role == "deep_researcher":
            return (
                "---\n"
                f'market_id: "{payload["market_id"]}"\n'
                "estimated_p: 0.6\n"
                "error: null\n"
                "---\n\n"
                "## Bull Thesis\n\nbody\n\n## Bear Thesis\n\nbody\n\n## Post-Mortem\n"
            )
        raise AssertionError(role)

    out = phases.phase3_qualitative_pipeline(vault, runner=runner)
    assert len(out) == 1
