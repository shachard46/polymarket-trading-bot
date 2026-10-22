"""Two full ticks in stub mode with a faked scraper / A-IQ must not crash."""

from __future__ import annotations


from obsidian_utils import ObsidianManager
from orchestrator import loop, phases, scraper
from orchestrator.scraper import MarketRow


def _row(mid: str, **over) -> MarketRow:
    md = {
        "yes_price": 0.4,
        "no_price": 0.6,
        "volume": 1_000.0,
        "liquidity": 20_000.0,
        "days_to_resolution": 30,
    }
    md.update(over)
    return MarketRow(market_id=mid, market_title=f"Will {mid}?", market_description="d", market_data=md)


def test_two_stub_ticks(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_ORCHESTRATOR_MODE", "stub")
    monkeypatch.setenv("OPENCLAW_VAULT_PATH", str(tmp_path))
    rows = {
        "ok1": _row("ok1"),
        "ok2": _row("ok2", yes_price=0.55, no_price=0.45),
        "far": _row("far", days_to_resolution=300),
        "arb": _row("arb", yes_price=0.4, no_price=0.5),
    }
    monkeypatch.setattr(scraper, "fetch_target_markets", lambda: list(rows.values()))
    monkeypatch.setattr(scraper, "fetch_market_row", lambda mid: rows.get(mid))
    monkeypatch.setattr(scraper, "fetch_resolution", lambda mid: None)
    monkeypatch.setattr(
        phases,
        "fetch_research_bundle",
        lambda qs: [{"query": q, "research_data": "stub data", "error": None} for q in qs],
    )
    vault = ObsidianManager()
    vault.cold_start_protocol()
    loop.run_pipeline_tick(vault)
    loop.run_pipeline_tick(vault)
    assert vault.read_filter_log("ok1")["triage"]["researchable"] is True
    assert vault.read_filter_log("far")["trigger"] == "too_far"
    assert vault.active_research_path("ok1").exists()
