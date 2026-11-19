"""Phase 3 FULL stage — state recovery, forced judge synthesis (no needs_more rounds), architect fallback."""

from __future__ import annotations

from typing import Any

import pytest

from config.trading_constants import ERROR_LOG_KEY, STATUS_INACTIVE, STATUS_KEY
from orchestrator import config as orch_config
from orchestrator import phases, scraper
from orchestrator.phases import p3_evidence, p3_full
from orchestrator.scraper import MarketRow
from tests.orchestrator.test_phase3_helpers import (
    HELPER_ROLES,
    STUB_MARKET_DATA,
    briefer_ok,
    deep_researcher_complete,
    deep_researcher_needs_more,
    helper_role_response,
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
        p3_evidence,
        "fetch_research_bundle",
        lambda queries: (calls.__setitem__("fetch", calls["fetch"] + 1) or []),
    )

    out = phases.phase3_qualitative_pipeline(vault, runner=runner)
    assert len(out) == 1
    assert calls["briefer"] == 0
    assert calls["fetch"] == 0


def test_phase3_judges_are_forced_and_never_loop(monkeypatch, vault):
    """No needs_more rounds: a FULL judge call always carries the forced-synthesis override."""
    _seed_filter(vault)
    _mock_row(monkeypatch)
    judge_payloads: list[dict[str, Any]] = []

    def runner(role: str, payload: dict[str, Any]) -> Any:
        if role == "briefer":
            return briefer_ok(payload)
        if role in HELPER_ROLES:
            return helper_role_response(role, payload)
        assert role == "deep_researcher", role
        if payload["mode"] == "judge":
            judge_payloads.append(payload)
        return deep_researcher_complete(payload)

    monkeypatch.setattr(
        p3_evidence,
        "fetch_research_bundle",
        lambda queries: [{"query": q, "research_data": "x", "error": None} for q in queries],
    )

    out = phases.phase3_qualitative_pipeline(vault, runner=runner)
    assert len(out) == 1
    assert len(judge_payloads) == 2 and all(p["system_override"] for p in judge_payloads)
    assert not hasattr(orch_config, "MAX_RESEARCH_ITERATIONS")


def test_phase3_forced_synthesis_disobedience_flags_inactive(monkeypatch, vault):
    _seed_filter(vault)
    _mock_row(monkeypatch)

    def runner(role: str, payload: dict[str, Any]) -> Any:
        if role == "briefer":
            return briefer_ok(payload)
        if role == "deep_researcher":
            if payload["mode"] == "quick":
                return deep_researcher_complete(payload)
            return deep_researcher_needs_more(payload)
        raise AssertionError(role)

    monkeypatch.setattr(
        p3_evidence,
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


def test_phase3_architect_error_falls_back_to_template_cruxes(monkeypatch, vault):
    """An architect failure never flags the market: code-built template cruxes (old fixed slots + horizon) run."""
    _seed_filter(vault)
    _mock_row(monkeypatch)
    fetched: list[str] = []

    def runner(role: str, payload: dict[str, Any]) -> Any:
        if role == "briefer":
            return {"market_id": payload["market_id"], "cruxes": [], "error": "cannot plan queries"}
        if role in HELPER_ROLES:
            return helper_role_response(role, payload)
        assert role == "deep_researcher", role
        return deep_researcher_complete(payload)

    monkeypatch.setattr(
        p3_evidence,
        "fetch_research_bundle",
        lambda queries: (fetched.extend(queries) or [{"query": q, "research_data": "x", "error": None} for q in queries]),
    )

    out = phases.phase3_qualitative_pipeline(vault, runner=runner)
    assert len(out) == 1
    assert not vault.is_market_inactive("0xabc", dir_key="filters")
    meta = vault.read_research_bundle_meta("0xabc")["crux_meta"]
    assert "architect_failed" in meta["flags"]
    assert {c["role"] for c in meta["cruxes"]} >= {"mechanism", "comparator", "horizon", "skeptic"}
    assert fetched  # the templates were searched


def test_phase3_survives_research_market_exception(monkeypatch, vault):
    vault.write_filter_log(
        "cond-x",
        {
            "market_id": "cond-x",
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
        lambda mid: MarketRow(
            market_id=mid, market_title="T", market_data=dict(STUB_MARKET_DATA)
        ),
    )

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(p3_full, "_research_market", boom)

    out = phases.phase3_qualitative_pipeline(
        vault,
        runner=lambda _role, _payload: {},
    )

    assert out == []
