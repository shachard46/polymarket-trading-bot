"""Phase 3 caps the qualitative queue at OPENCLAW_TOP_MARKETS (default 20)."""

from __future__ import annotations

from obsidian_utils import ObsidianManager
from orchestrator import phases


def test_build_phase3_queue_sorts_and_caps(monkeypatch, tmp_path):
    vault = ObsidianManager(vault_base=tmp_path)
    monkeypatch.setattr(phases, "top_qualitative_markets", lambda: 2)

    mult_by_id = {"low": 1.0, "high": 5.0, "mid": 3.0}
    for mid, mult in mult_by_id.items():
        vault.write_filter_log(
            mid,
            {
                "market_id": mid,
                "passed": True,
                "trigger": "stub",
                "confidence_multiplier": mult,
                "details": "ok",
                "error": None,
            },
        )

    queue = phases.build_phase3_queue(vault)
    assert [c.market_id for c in queue] == ["high", "mid"]
