"""Phase 3 iterative Hub loop — state recovery, needs_more_data, forced synthesis."""

from __future__ import annotations

from typing import Any

import pytest

from config.trading_constants import STATUS_INACTIVE, STATUS_KEY
from orchestrator import phases, scraper
from orchestrator.scraper import MarketRow
from tests.orchestrator.test_phase3_helpers import (
    briefer_ok,
    deep_researcher_complete,
    deep_researcher_needs_more,
)


@pytest.fixture()
def vault(tmp_path):
    from obsidian_utils import ObsidianManager

    return ObsidianManager(vault_base=tmp_path)


def _seed_filter(vault, market_id: str = "0xabc") -> None:
    vault.write_filter_log(
        market_id,
        {
            "market_id": market_id,
            "passed": True,
            "trigger": "stub",
            "confidence_multiplier": 1.0,
            "details": "ok",
            "error": None,
        },
    )


def _mock_row(monkeypatch, market_id: str = "0xabc") -> None:
    monkeypatch.setattr(
        scraper,
        "fetch_market_row",
        lambda mid: MarketRow(
            market_id=mid,
            market_title="Title",
            market_description="Desc",
            market_data={"yes_price": 0.42},
        ),
    )


def test_phase3_resumes_from_salvaged_bundle_without_briefer_or_fetch(
    monkeypatch, vault
):
    _seed_filter(vault)
    _mock_row(monkeypatch)
    vault.write_research_bundle(
        "0xabc",
        [{"query": "prior", "research_data": "saved", "error": None}],
    )

    calls = {"briefer": 0, "fetch": 0}

    def runner(role: str, payload: dict[str, Any]) -> Any:
        if role == "briefer":
            calls["briefer"] += 1
            return briefer_ok(payload)
        if role == "deep_researcher":
            return deep_researcher_complete(payload)
        raise AssertionError(role)

    monkeypatch.setattr(
        phases,
        "fetch_research_bundle",
        lambda queries: (calls.__setitem__("fetch", calls["fetch"] + 1) or []),
    )

    out = phases.phase3_qualitative_pipeline(vault, runner=runner)
    assert len(out) == 1
    assert calls["briefer"] == 0
    assert calls["fetch"] == 0


def test_phase3_needs_more_data_then_complete(monkeypatch, vault):
    _seed_filter(vault)
    _mock_row(monkeypatch)
    dr_calls = {"n": 0}

    def runner(role: str, payload: dict[str, Any]) -> Any:
        if role == "briefer":
            return briefer_ok(payload)
        if role == "deep_researcher":
            dr_calls["n"] += 1
            if dr_calls["n"] == 1:
                return deep_researcher_needs_more(payload)
            return deep_researcher_complete(payload)
        raise AssertionError(role)

    monkeypatch.setattr(
        phases,
        "fetch_research_bundle",
        lambda queries: [
            {"query": q, "research_data": f"data:{q}", "error": None} for q in queries
        ],
    )

    out = phases.phase3_qualitative_pipeline(vault, runner=runner)
    assert len(out) == 1
    bundle = vault.read_research_bundle("0xabc")
    assert bundle is not None
    assert len(bundle) >= 2


def test_phase3_forced_synthesis_on_iteration_cap(monkeypatch, vault):
    _seed_filter(vault)
    _mock_row(monkeypatch)

    def runner(role: str, payload: dict[str, Any]) -> Any:
        if role == "briefer":
            return briefer_ok(payload)
        if role == "deep_researcher":
            if payload.get("system_override"):
                return deep_researcher_complete(payload)
            return deep_researcher_needs_more(payload)
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


def test_phase3_forced_synthesis_disobedience_flags_inactive(monkeypatch, vault):
    _seed_filter(vault)
    _mock_row(monkeypatch)

    def runner(role: str, payload: dict[str, Any]) -> Any:
        if role == "briefer":
            return briefer_ok(payload)
        if role == "deep_researcher":
            return deep_researcher_needs_more(payload)
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
    filt = vault.read_filter_log("0xabc")
    assert filt is not None
    assert filt.get(STATUS_KEY) == STATUS_INACTIVE
