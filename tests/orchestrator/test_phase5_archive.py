"""F8 regression: trade JSON is archived after a successful post-mortem."""

from __future__ import annotations

from typing import Any

import pytest

from obsidian_utils import ObsidianManager
from orchestrator import phases, scraper


@pytest.fixture()
def vault(tmp_path):
    return ObsidianManager(vault_base=tmp_path)


def _seed_resolved_market(vault: ObsidianManager, market_id: str) -> None:
    vault.write_trade_log(
        market_id,
        {
            "market_id": market_id,
            "allocation_usd": 0.0,
            "executed": False,
            "transaction_hash": None,
            "error": None,
        },
    )
    vault.write_research_report(
        market_id,
        {"market_id": market_id, "estimated_p": 0.5, "error": None},
        "## Bull Thesis\n\nb\n\n## Bear Thesis\n\nb\n\n## Post-Mortem\n",
    )


def _runner(role: str, payload: dict[str, Any]) -> dict[str, Any]:
    assert role == "post_mortem_analyst"
    return {
        "market_id": payload["market_id"],
        "post_mortem_analysis": "stub paragraph",
        "error": None,
    }


def test_post_mortem_success_archives_trade(vault, monkeypatch):
    _seed_resolved_market(vault, "0xabc")
    monkeypatch.setattr(
        scraper,
        "fetch_resolution",
        lambda mid: {"market_id": mid, "status": "closed", "outcome": "yes"},
    )

    phases.phase5_resolution_and_post_mortem(vault, runner=_runner)

    open_trades = [p.name for p in vault.iter_open_trades()]
    assert "0xabc.json" not in open_trades
    archived = list((vault._dirs["trades"] / "_resolved").glob("0xabc*.json"))
    assert archived, "expected archived trade JSON"


def test_subsequent_tick_does_not_re_resolve_archived_trade(vault, monkeypatch):
    _seed_resolved_market(vault, "0xabc")
    calls: list[str] = []

    def fake_resolution(mid: str) -> dict[str, Any]:
        calls.append(mid)
        return {"market_id": mid, "status": "closed", "outcome": "yes"}

    monkeypatch.setattr(scraper, "fetch_resolution", fake_resolution)

    phases.phase5_resolution_and_post_mortem(vault, runner=_runner)
    phases.phase5_resolution_and_post_mortem(vault, runner=_runner)

    assert calls == ["0xabc"], f"expected exactly one resolution call, got {calls}"
