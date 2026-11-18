"""Market-integrity wiring: Phase 2 fetch + gate, description hash, carry to later rows, stats bands."""

from __future__ import annotations

import time

import pytest

from obsidian_utils import ObsidianManager
from orchestrator import ledger, phases, polymarket_api, stats
from orchestrator.phases import common
from tests.orchestrator.test_phase2_gate_triage import Triage, mk


@pytest.fixture()
def vault(tmp_path):
    v = ObsidianManager(vault_base=tmp_path)
    v.cold_start_protocol()
    return v


def _wash_trades():
    now = time.time()
    return [
        {"proxy_wallet": "0xw", "side": "BUY", "size": 100.0, "price": 0.5, "timestamp": now - i * 3600}
        for i in range(12)
    ]


def _rows(vault, stage):
    return [r for r in ledger.iter_rows(vault) if r["stage"] == stage]


def test_wallet_dominated_rejects_with_backoff_and_ledger(vault, monkeypatch):
    monkeypatch.setattr(polymarket_api, "fetch_trades", lambda cid: _wash_trades())
    tri = Triage()
    phases.phase2_quantitative_routing(vault, [mk("a", yes_token_id="t")], runner=tri)
    assert tri.calls == []  # rejected before the paid triage call
    (row,) = _rows(vault, "gate_reject")
    assert row["reason"] == "wallet_dominated"
    assert row["integ_top1_wallet_share"] == 1.0 and "integ_score" in row
    rec = vault.read_filter_log("a")
    assert rec["trigger"] == "wallet_dominated" and rec["fail_count"] == 1 and rec["next_check_at"]
    assert rec["integrity"]["top1_wallet_share"] == 1.0
    cov = _rows(vault, "signal_coverage")[0]
    assert cov["trades"] == {"missing": 0, "total": 1}
    assert cov["price_history"] == {"missing": 1, "total": 1} and cov["book"] == {"missing": 1, "total": 1}


def test_missing_data_passes_and_records_integrity(vault):
    tri = Triage()
    phases.phase2_quantitative_routing(vault, [mk("a", description="rules v1")], runner=tri)
    assert len(tri.calls) == 1 and _rows(vault, "gate_reject") == []
    rec = vault.read_filter_log("a")
    assert rec["passed"] is True and rec["description_first_hash"]
    assert isinstance(rec["integrity"], dict) and "flags" in rec["integrity"]


def test_description_hash_persists_and_rules_changed_fires_tick2(vault):
    phases.phase2_quantitative_routing(vault, [mk("a", description="rules v1")], runner=Triage())
    first = vault.read_filter_log("a")["description_first_hash"]
    assert first
    phases.phase2_quantitative_routing(vault, [mk("a", description="rules v1")], runner=Triage())
    assert vault.read_filter_log("a")["description_first_hash"] == first
    phases.phase2_quantitative_routing(vault, [mk("a", description="rules v2 EDITED")], runner=Triage())
    (row,) = _rows(vault, "gate_reject")
    assert row["reason"] == "rules_changed"
    rec = vault.read_filter_log("a")
    assert rec["trigger"] == "rules_changed" and rec["description_first_hash"] == first  # not overwritten


def test_first_hash_kept_by_filter_state_writes(vault):
    common._write_filter_state(vault, "a", None, description_first_hash="h1")
    prior = vault.read_filter_log("a")
    common._write_filter_state(vault, "a", prior, description_first_hash="h2", passed=False)
    assert vault.read_filter_log("a")["description_first_hash"] == "h1"


def test_integ_score_carried_to_later_rows(vault):
    phases.phase2_quantitative_routing(vault, [mk("a", description="d")], runner=Triage())
    rec = vault.read_filter_log("a")
    md = common._carry_filter_signals({"yes_price": 0.4}, rec)
    fields = common._ledger_market_fields(md)
    assert fields["integ_score"] == rec["integrity"]["score"]
    assert fields["integ_flags"] == rec["integrity"]["flags"]
    assert common._ledger_market_fields({})["integ_score"] is None


def _res(m, y):
    return {"stage": "resolution", "market_id": m, "outcome_yes": y}


def test_stats_integrity_bands_and_rejects():
    rows = [
        {"stage": "research_skip", "market_id": "hi", "q": 0.9, "integ_score": 0.95}, _res("hi", 1),
        {"stage": "research_skip", "market_id": "mid", "q": 0.5, "integ_score": 0.6}, _res("mid", 1),
        {"stage": "research_skip", "market_id": "lo", "q": 0.2, "integ_score": 0.2}, _res("lo", 0),
        {"stage": "research_skip", "market_id": "unk", "q": 0.3}, _res("unk", 1),
        {"stage": "gate_reject", "market_id": "rc", "q": 0.8, "reason": "rules_changed", "integ_score": 0.7},
        _res("rc", 0),
    ]
    mi = stats.market_integrity_stats(rows)
    assert mi["bands"][">=0.8"][0] == 1 and mi["bands"]["0.5-0.8"][0] == 2
    assert mi["bands"]["<0.5"][0] == 1 and mi["bands"]["unknown"][0] == 1
    assert mi["rejects"]["rules_changed"] == (1, pytest.approx(0.64))
    assert mi["rejects"]["ramp_thin"][0] == 0
    s = stats.compute_stats(rows)
    assert "Market integrity" in stats.render_stats(s) and s["n_scored"] == 0


def test_day_change_filled_from_history_and_suspicious_move(vault, monkeypatch):
    day = 86400
    now = int(time.time())
    hist = [(now - 2 * day, 0.30), (now - day, 0.30), (now, 0.60)]
    monkeypatch.setattr(polymarket_api, "fetch_price_history", lambda tok: hist)
    tri = Triage()
    phases.phase2_quantitative_routing(
        vault, [mk("a", yes_token_id="t", volume_24h=100.0)], runner=tri
    )
    (row,) = _rows(vault, "gate_reject")
    assert row["reason"] == "suspicious_move" and row["day_price_change"] == pytest.approx(0.30)
    cov = _rows(vault, "signal_coverage")[0]
    assert cov["day_price_change"] == {"missing": 0, "total": 1}


def _p5_run(vault, monkeypatch):
    from orchestrator import scraper

    phases.phase2_quantitative_routing(vault, [mk("a", description="d")], runner=Triage())
    rec = vault.read_filter_log("a")
    ledger.record(vault, "a", "research_skip", q=0.4, reason="x")
    vault.write_trade_log("a", {
        "market_id": "a", "allocation_usd": 0.0, "executed": False,
        "transaction_hash": None, "error": None,
    })
    vault.write_research_report(
        "a", {"market_id": "a", "estimated_p": 0.5, "error": None},
        "## Bull Thesis\n\nb\n\n## Bear Thesis\n\nb\n\n## Post-Mortem\n",
    )
    monkeypatch.setattr(
        scraper, "fetch_resolution", lambda mid: {"market_id": mid, "status": "closed", "outcome": "yes"}
    )

    def runner(role, payload):
        return {"market_id": payload["market_id"], "post_mortem_analysis": "x", "error": None}

    phases.phase5_resolution_and_post_mortem(vault, runner=runner)
    return rec["integrity"]


def test_p5_resolution_row_carries_integ_fields(vault, monkeypatch):
    integ = _p5_run(vault, monkeypatch)
    (row,) = _rows(vault, "resolution")
    assert row["integ_score"] == integ["score"] and row["integ_flags"] == integ["flags"]


def test_p5_post_mortem_row_carries_integ_fields(vault, monkeypatch):
    integ = _p5_run(vault, monkeypatch)
    (row,) = _rows(vault, "post_mortem")
    assert row["integ_score"] == integ["score"] and row["integ_flags"] == integ["flags"]
