"""Phase 3 must not reference ``researched_row`` after a swallowed quarantine exception."""

from __future__ import annotations

from typing import Any

import pytest

from obsidian_utils import ObsidianManager
from orchestrator import phases, scraper
from orchestrator.scraper import MarketRow
from tests.orchestrator.test_phase3_helpers import STUB_MARKET_DATA


def test_phase3_survives_research_market_exception(monkeypatch, tmp_path):
    vault = ObsidianManager(vault_base=tmp_path)
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

    monkeypatch.setattr(phases, "_research_market", boom)

    out = phases.phase3_qualitative_pipeline(
        vault,
        runner=lambda _role, _payload: {},
    )

    assert out == []
