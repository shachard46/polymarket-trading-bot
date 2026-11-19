"""Signal coverage: per-tick ledger row, alert logging, stats rendering and exclusion."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator import ledger, phases, polymarket_api, stats
from tests.orchestrator.test_phase2_gate_triage import Triage, mk


def _cov(vault):
    return [r for r in ledger.iter_rows(vault) if r["stage"] == "signal_coverage"]


def test_coverage_row_counts_and_alert(vault, monkeypatch, caplog):
    monkeypatch.setattr(polymarket_api, "fetch_holder_concentration", lambda cid: None)
    full = dict(volume_24h=10.0, day_price_change=0.01, yes_bid=0.39, yes_ask=0.41)
    markets = [mk("a", **full), mk("b", volume_24h=10.0, yes_bid=0.39, yes_ask=0.41), mk("c")]
    with caplog.at_level(logging.ERROR):
        phases.phase2_quantitative_routing(vault, markets, runner=Triage())
    (row,) = _cov(vault)
    assert row["market_id"] == "_tick"
    assert row["volume_24h"] == {"missing": 1, "total": 3}
    assert row["day_price_change"] == {"missing": 2, "total": 3}
    assert row["bid_ask"] == {"missing": 1, "total": 3}
    assert row["holders"] == {"missing": 3, "total": 3}
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("day_price_change" in m for m in msgs) and any("holders" in m for m in msgs)
    assert not any("volume_24h" in m for m in msgs)  # 1/3 <= alert fraction


def test_no_row_without_gated_markets(vault):
    phases.phase2_quantitative_routing(vault, [], runner=Triage())
    assert _cov(vault) == []


def test_holders_counted_only_when_attempted(vault, monkeypatch):
    monkeypatch.setenv("PMBOT_GATE_BUDGET", "1")
    monkeypatch.setattr(polymarket_api, "fetch_holder_concentration",
                        lambda cid: {"top1": 0.1, "top3": 0.2, "basis": "supply"})
    phases.phase2_quantitative_routing(vault, [mk("a"), mk("b")], runner=Triage())
    (row,) = _cov(vault)
    assert row["holders"] == {"missing": 0, "total": 1} and row["volume_24h"]["total"] == 2


def _tick(ts, **sigs):
    return {"stage": "signal_coverage", "market_id": "_tick", "ts": ts.isoformat(), **sigs}


def test_stats_coverage_latest_week_flag_and_exclusion():
    now = datetime.now(timezone.utc)
    rows = [
        _tick(now - timedelta(days=30), volume_24h={"missing": 9, "total": 10}),
        _tick(now - timedelta(days=1), volume_24h={"missing": 1, "total": 10}),
        _tick(now, volume_24h={"missing": 8, "total": 10}, holders={"missing": 0, "total": 5}),
        {"stage": "resolution", "market_id": "_tick", "outcome_yes": 1},
    ]
    c = stats.signal_coverage_stats(rows, now)["signals"]
    assert c["volume_24h"] == {"latest": (8, 10), "week": (9, 20)}
    assert c["holders"]["latest"] == (0, 5)
    s = stats.compute_stats(rows)
    assert s["n_scored"] == 0 and s["false_negative"][0] == 0
    md = stats.render_stats(s)
    assert "Signal coverage" in md and "80.0% ⚠" in md


def test_stats_gate_flags_vs_passed():
    rows = []
    spec = [("g1", "suspicious_move", 1, 0.2, None), ("g2", "concentrated", 0, 0.4, "supply"),
            ("g3", "too_soon", 0, 0.5, None), ("ok", None, 1, 0.9, None)]
    for mid, reason, y, q, basis in spec:
        if reason:
            rows.append({"stage": "gate_reject", "market_id": mid, "reason": reason, "q": q, "holder_basis": basis})
        else:
            rows.append({"stage": "research_skip", "market_id": mid, "q": q})
        rows.append({"stage": "resolution", "market_id": mid, "outcome_yes": y})
    g = stats.gate_flag_stats(rows)
    assert g["suspicious_move"] == (1, pytest.approx(0.64))
    assert g["concentrated"] == (1, pytest.approx(0.16))
    assert g["concentrated/supply"][0] == 1
    assert g["passed_gate"] == (1, pytest.approx(0.01))
    assert "too_soon" not in g and "wide_spread" not in g
    assert "Gate flags vs market calibration" in stats.render_stats(stats.compute_stats(rows))
