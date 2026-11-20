"""Ledger sweep ordering for phase 5."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestrator import ledger, scraper
from orchestrator.phases import p5_resolution as p5

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


def _iso(days):
    return (NOW + timedelta(days=days)).isoformat()


def _rec(vault, mid, stage, days=None, **kw):
    if days is not None:
        kw["end_date"] = _iso(days)
    ledger.record(vault, mid, stage, **kw)


def test_order_future_skip_tier_and_age(vault):
    _rec(vault, "fut", "traded", 5)
    _rec(vault, "g_old", "gate_reject", -10)
    _rec(vault, "t_new", "traded", -1)
    _rec(vault, "t_old", "below_edge", -3)
    _rec(vault, "tr", "triage_reject", -20)
    order, c = p5.ledger_sweep_order(vault, NOW)
    assert order == ["t_old", "t_new", "tr", "g_old"]
    assert c == {"unresolved": 5, "due": 4, "skipped_future": 1, "unknown_end": 0}


def test_unknown_after_due_and_rotates(vault, monkeypatch):
    _rec(vault, "due", "gate_reject", -1)
    _rec(vault, "u1", "gate_reject")
    _rec(vault, "u2", "gate_reject")
    monkeypatch.setattr(scraper, "fetch_resolution", lambda m: None)
    monkeypatch.setenv("PMBOT_LEDGER_RESOLVE_LIMIT", "2")
    assert p5.ledger_sweep_order(vault, NOW)[0] == ["due", "u1", "u2"]
    p5._resolve_ledger_markets(vault, NOW)  # checks due, u1
    assert p5.ledger_sweep_order(vault, NOW)[0] == ["due", "u2", "u1"]


def test_limit_and_resolution(vault, monkeypatch):
    for i in range(5):
        _rec(vault, f"m{i}", "traded", -5 + i)
    _rec(vault, "fut", "traded", 9)
    seen = []
    def fake(m):
        seen.append(m)
        return {"outcome": "Yes", "raw_outcome": "Yes"} if m == "m0" else None
    monkeypatch.setattr(scraper, "fetch_resolution", fake)
    monkeypatch.setenv("PMBOT_LEDGER_RESOLVE_LIMIT", "3")
    p5._resolve_ledger_markets(vault, NOW)
    assert seen == ["m0", "m1", "m2"]
    assert "m0" not in ledger.unresolved_market_ids(vault)
