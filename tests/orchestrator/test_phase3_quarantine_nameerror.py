"""Phase 3 must not reference ``researched_row`` after a swallowed quarantine exception."""

from __future__ import annotations

from typing import Any

from obsidian_utils import ObsidianManager
from orchestrator import phases


def test_phase3_survives_research_market_exception(monkeypatch, tmp_path):
    vault = ObsidianManager(vault_base=tmp_path)

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(phases, "_research_market", boom)

    out = phases.phase3_qualitative_pipeline(
        vault,
        [{"market_id": "cond-x", "market_title": "T", "market_data": {}}],
        runner=lambda _role, _payload: {},
    )

    assert out == []
