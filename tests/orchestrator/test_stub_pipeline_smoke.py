"""Two full ticks in stub mode with a faked scraper / A-IQ must not crash."""

from __future__ import annotations

from obsidian_utils import ObsidianManager
from orchestrator import ledger, loop, polymarket_api, scraper
from orchestrator.phases import p3_evidence
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
    monkeypatch.setenv("PMBOT_MODE", "stub")
    monkeypatch.setenv("PMBOT_VAULT_PATH", str(tmp_path))
    rows = {
        "ok1": _row("ok1", yes_price=0.26, no_price=0.74),
        "ok2": _row("ok2", yes_price=0.55, no_price=0.45),
        "far": _row("far", days_to_resolution=300),
        "arb": _row("arb", yes_price=0.4, no_price=0.5),
    }
    monkeypatch.setattr(scraper, "fetch_target_markets", lambda knobs=None: list(rows.values()))
    monkeypatch.setattr(scraper, "fetch_market_row", lambda mid: rows.get(mid))
    monkeypatch.setattr(scraper, "fetch_resolution", lambda mid: None)
    monkeypatch.setattr(polymarket_api, "fetch_holder_concentration", lambda cid: None)
    monkeypatch.setattr(
        p3_evidence,
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
    # Stub Deep Researcher says p=0.55 (range 0.51-0.59): ok1 (q=0.26) diverges, is
    # escalated, researched in full and paper-traded; ok2 (q=0.55) is inside the range.
    fm = vault.read_market_record("ok1", "active")
    assert fm["estimated_p"] == 0.55 and fm["q_at_research"] == 0.26
    trade = vault.read_trade_log_dict("ok1")
    assert trade is not None and trade["side"] == "YES" and trade["allocation_usd"] > 0
    assert trade["executed"] is False and str(trade["transaction_hash"]).startswith("PAPER-")
    assert not vault.active_research_path("ok2").exists()
    stages = {(r["market_id"], r["stage"], r.get("reason")) for r in ledger.iter_rows(vault)}
    assert ("ok2", "research_skip", "no_divergence") in stages
    assert ("ok1", "traded", None) in stages
    # tick 2 did not redo the skipped quick research for ok2
    skips = [r for r in ledger.iter_rows(vault) if r["market_id"] == "ok2" and r["stage"] == "research_skip"]
    assert len(skips) == 1
