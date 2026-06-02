"""F2 regression: real market_title/market_description/market_data flow end-to-end."""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator import phases, scraper
from orchestrator.scraper import (
    MarketRow,
    market_data_hydration_error,
    _market_row_from_scraper,
    _market_snapshot_from_scraper_row,
)
from tests.orchestrator.test_phase3_helpers import briefer_ok, deep_researcher_complete


@pytest.fixture()
def vault(tmp_path):
    from obsidian_utils import ObsidianManager

    return ObsidianManager(vault_base=tmp_path)


def test_market_row_from_scraper_preserves_title_description_and_snapshot():
    raw = {
        "market_id": "0xabc",
        "question": "Will X happen by 2027?",
        "description": "Long form description.",
        "yes_price": 0.42,
        "no_price": 0.58,
        "volume": 1234.0,
        "liquidity": 5678.0,
        "midpoint": 0.50,
        "spread": 0.02,
        "last_trade_price": 0.43,
    }
    row = _market_row_from_scraper(raw)
    assert row is not None
    assert row.market_id == "0xabc"
    assert row.market_title == "Will X happen by 2027?"
    assert row.market_description == "Long form description."
    assert row.market_data["yes_price"] == 0.42
    assert row.market_data["volume"] == 1234.0
    assert row.market_data["liquidity"] == 5678.0


def test_market_row_from_scraper_reads_nested_latest_change():
    raw = {
        "market_id": "0xabc",
        "question": "Will X happen by 2027?",
        "description": "Long form description.",
        "latest_change": {
            "datetime": "2026-06-01T12:00:00Z",
            "yes_price": 0.42,
            "no_price": 0.58,
            "volume": 1234.0,
            "liquidity": 5678.0,
            "last_trade_price": 0.43,
            "midpoint": 0.50,
            "spread": 0.02,
        },
    }
    row = _market_row_from_scraper(raw)
    assert row is not None
    assert row.market_data["yes_price"] == 0.42
    assert row.market_data["midpoint"] == 0.50
    assert row.market_data["volume"] == 1234.0


def test_market_snapshot_prefers_top_level_over_latest_change():
    raw = {
        "yes_price": 0.10,
        "latest_change": {"yes_price": 0.99},
    }
    snapshot = _market_snapshot_from_scraper_row(raw)
    assert snapshot["yes_price"] == 0.10


def test_market_snapshot_derives_days_to_resolution_from_extra_info_end_date():
    raw = {
        "extra_info": '{"endDateIso": "2026-12-31T00:00:00Z"}',
        "latest_change": {
            "yes_price": 0.42,
            "volume": 100.0,
            "liquidity": 200.0,
        },
    }
    snapshot = _market_snapshot_from_scraper_row(raw)
    assert snapshot["end_date"] == "2026-12-31T00:00:00Z"
    assert isinstance(snapshot["days_to_resolution"], int)
    assert snapshot["days_to_resolution"] >= 1


def test_market_snapshot_derives_end_date_from_parsed_extra_info_dict():
    raw = {
        "extra_info": {"endDateIso": "2026-08-18"},
        "latest_change": {"yes_price": 0.42, "volume": 100.0, "liquidity": 200.0},
    }
    snapshot = _market_snapshot_from_scraper_row(raw)
    assert snapshot["end_date"] == "2026-08-18"
    assert snapshot["days_to_resolution"] >= 1


def test_market_data_hydration_error_when_pricing_missing():
    assert market_data_hydration_error({}) is not None
    assert market_data_hydration_error({"volume": 1.0, "liquidity": 2.0}) is not None


def test_market_data_hydration_error_when_volume_liquidity_missing():
    assert market_data_hydration_error({"yes_price": 0.42}) is not None


def test_market_data_hydration_error_when_volume_nonpositive():
    err = market_data_hydration_error(
        {"yes_price": 0.42, "volume": 0.0, "liquidity": 200.0}
    )
    assert err is not None
    assert "volume" in err
    assert "poly-scan scan --market" in err


def test_market_data_hydration_error_when_liquidity_nonpositive():
    err = market_data_hydration_error(
        {"yes_price": 0.42, "volume": 100.0, "liquidity": -1.0}
    )
    assert err is not None
    assert "liquidity" in err


def test_market_data_hydration_error_ok_with_q_and_liquidity_fields():
    assert market_data_hydration_error(
        {"yes_price": 0.42, "volume": 100.0, "liquidity": 200.0}
    ) is None


def test_market_row_drops_rows_without_title():
    assert _market_row_from_scraper({"market_id": "x", "question": ""}) is None
    assert _market_row_from_scraper({"market_id": "", "question": "Q"}) is None


def test_phase4_fail_fast_on_empty_market_data(vault):
    vault.cold_start_protocol()
    spawned: list[str] = []

    def runner(role: str, payload: dict[str, Any]) -> dict[str, Any]:
        spawned.append(role)
        raise AssertionError("executioner should not be spawned")

    phases.phase4_execution(
        vault,
        [{"market_id": "0xempty", "p_value": 0.5, "market_data": {}}],
        runner=runner,
    )
    assert spawned == []
    trade = vault.read_trade_log_dict("0xempty")
    assert trade is None


def test_phase2_writes_filter_for_phase3_scan(monkeypatch, vault):
    vault.cold_start_protocol()
    market = MarketRow(
        market_id="0xabc",
        market_title="Will X happen?",
        market_description="Background.",
        market_data={"yes_price": 0.42, "volume": 100.0, "liquidity": 200.0},
    )

    def evaluator_runner(role: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "market_id": payload["market_id"],
            "passed": True,
            "trigger": "breakout",
            "confidence_multiplier": 1.0,
            "details": "ok",
            "error": None,
        }

    phases.phase2_quantitative_routing(vault, [market], runner=evaluator_runner)
    filt = vault.read_filter_log("0xabc")
    assert filt is not None
    assert filt["passed"] is True

    queue = phases.build_phase3_queue(vault)
    assert len(queue) == 1
    assert queue[0].market_id == "0xabc"


def test_phase3_passes_real_title_and_description_to_briefer(monkeypatch, vault):
    captured: dict[str, Any] = {}

    vault.write_filter_log(
        "0xabc",
        {
            "market_id": "0xabc",
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
            market_id=mid,
            market_title="Real title",
            market_description="Real desc.",
            market_data={"yes_price": 0.42, "volume": 1.0, "liquidity": 2.0},
        ),
    )

    def runner(role: str, payload: dict[str, Any]) -> Any:
        captured.setdefault(role, []).append(payload)
        if role == "briefer":
            return briefer_ok(payload)
        if role == "deep_researcher":
            return deep_researcher_complete(payload)
        raise AssertionError(f"unexpected role {role}")

    monkeypatch.setattr(
        phases,
        "fetch_research_bundle",
        lambda queries: [
            {"query": q, "research_data": "stub", "error": None} for q in queries
        ],
    )

    out = phases.phase3_qualitative_pipeline(vault, runner=runner)
    assert len(out) == 1

    brief_payload = captured["briefer"][0]
    assert brief_payload["market_title"] == "Real title"
    assert brief_payload["market_description"] == "Real desc."

    dr_payload = captured["deep_researcher"][0]
    assert dr_payload["market_data"]["yes_price"] == 0.42
    assert isinstance(dr_payload["research_bundle"], list)

    forwarded = out[0]
    assert forwarded["market_data"]["yes_price"] == 0.42
