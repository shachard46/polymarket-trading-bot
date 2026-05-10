"""F2 regression: real market_title/market_description/market_data flow end-to-end."""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator import phases, scraper
from orchestrator.scraper import MarketRow, _market_row_from_scraper


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


def test_market_row_drops_rows_without_title():
    assert _market_row_from_scraper({"market_id": "x", "question": ""}) is None
    assert _market_row_from_scraper({"market_id": "", "question": "Q"}) is None


def test_phase2_forwards_full_market_row_to_phase3(monkeypatch, vault):
    market = MarketRow(
        market_id="0xabc",
        market_title="Will X happen?",
        market_description="Background.",
        market_data={"yes_price": 0.42, "volume": 100.0, "liquidity": 200.0},
    )
    monkeypatch.setattr(scraper, "get_market_trends", lambda mid, limit: [
        {"datetime": "2026-01-01T00:00:00Z", "yes_price": 0.5},
    ])
    monkeypatch.setattr(scraper, "trends_limit_for_filters", lambda: 200)

    def evaluator_runner(role: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "market_id": payload["market_id"],
            "passed": True,
            "trigger": "breakout",
            "confidence_multiplier": 1.0,
            "details": "ok",
            "error": None,
        }

    passed, refresh = phases.phase2_quantitative_routing(vault, [market], runner=evaluator_runner)
    rows = phases.merge_phase3_inputs(passed, refresh, 100)
    assert len(rows) == 1
    forwarded = rows[0]
    assert forwarded["market_id"] == "0xabc"
    assert forwarded["market_title"] == "Will X happen?"
    assert forwarded["market_description"] == "Background."
    assert forwarded["market_data"]["yes_price"] == 0.42
    assert forwarded["market_data"]["liquidity"] == 200.0
    assert forwarded["evaluator_output"]["passed"] is True


def test_phase3_passes_real_title_and_description_to_briefer(monkeypatch, vault):
    captured: dict[str, Any] = {}

    def runner(role: str, payload: dict[str, Any]) -> Any:
        captured.setdefault(role, []).append(payload)
        if role == "briefer":
            return {
                "market_id": payload["market_id"],
                "summary": "stub summary",
                "error": None,
            }
        if role == "deep_researcher":
            return (
                "---\n"
                f"market_id: \"{payload['market_id']}\"\n"
                "estimated_p: 0.5\n"
                "error: null\n"
                "---\n\n"
                "## Bull Thesis\n\nbody\n\n## Bear Thesis\n\nbody\n\n## Post-Mortem\n"
            )
        raise AssertionError(f"unexpected role {role}")

    rows = [
        {
            "market_id": "0xabc",
            "market_title": "Real title",
            "market_description": "Real desc.",
            "market_data": {"yes_price": 0.42, "volume": 1.0, "liquidity": 2.0},
        }
    ]
    out = phases.phase3_qualitative_pipeline(vault, rows, runner=runner)
    assert len(out) == 1

    brief_payload = captured["briefer"][0]
    assert brief_payload["market_title"] == "Real title"
    assert brief_payload["market_description"] == "Real desc."

    dr_payload = captured["deep_researcher"][0]
    assert dr_payload["market_data"]["yes_price"] == 0.42
    assert dr_payload["context_summary"] == "stub summary"

    forwarded = out[0]
    assert forwarded["market_data"]["yes_price"] == 0.42
