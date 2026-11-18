"""Triage shadow: stable selection, pending marker at reject time, measure-only quick run in Phase 3."""

from __future__ import annotations

import pytest

from config.trading_constants import TRIAGE_SHADOW_MAX_PER_TICK, TRIAGE_SHADOW_RATE
from orchestrator import phases, polymarket_api, stats
from orchestrator.phases import p2_routing
from tests.orchestrator.helpers import _rows
from tests.orchestrator.test_phase2_gate_triage import Triage, mk
from tests.orchestrator.test_research_redesign import World


@pytest.fixture(autouse=True)
def _no_holder_network(monkeypatch):
    monkeypatch.setattr(polymarket_api, "fetch_holder_concentration", lambda cid: None)


@pytest.fixture()
def world(monkeypatch, vault):
    return World(monkeypatch, vault)


def _ids(want: bool, n: int) -> list[str]:
    out = [m for m in (f"t{i}" for i in range(3000)) if p2_routing.is_triage_shadow(m) is want]
    return out[:n]


def _retired(vault, mid, *, pending=True, risk="low", reason="coin flip"):
    triage = {"researchable": risk == "high", "category": "politics", "reason": reason,
              "manipulation_risk": risk}
    if pending is not None:
        triage["triage_shadow"] = "pending" if pending else "done"
    vault.write_filter_log(mid, {
        "market_id": mid, "passed": False, "trigger": "triage_reject", "confidence_multiplier": 0.0,
        "details": "x", "error": None, "status": "retired", "triage": triage,
    })


def test_selection_is_stable_and_near_rate():
    ids = [f"0x{i:05x}" for i in range(6000)]
    flags = [p2_routing.is_triage_shadow(m) for m in ids]
    assert flags == [p2_routing.is_triage_shadow(m) for m in ids]
    assert abs(sum(flags) / len(ids) - TRIAGE_SHADOW_RATE) < 0.03


@pytest.mark.parametrize("risk,verdict", [("low", False), ("high", True)])
def test_reject_marks_pending_only_for_selected(vault, risk, verdict):
    sel, other = _ids(True, 1)[0], _ids(False, 1)[0]
    t = Triage(verdicts={sel: verdict, other: verdict}, risk=risk)
    phases.phase2_quantitative_routing(vault, [mk(sel), mk(other)], runner=t)
    assert vault.read_filter_log(sel)["triage"]["triage_shadow"] == "pending"
    assert "triage_shadow" not in vault.read_filter_log(other)["triage"]
    assert vault.read_filter_log(sel)["status"] == "retired"


def test_pending_retired_market_gets_one_measure_only_row(world, vault):
    mid = _ids(True, 1)[0]
    world.market(mid, 0.5)
    _retired(vault, mid)
    world.dr_p = (0.8, 0.7, 0.9)
    assert phases.phase3_qualitative_pipeline(vault, runner=world.runner) == []
    (row,) = _rows(vault, "triage_shadow")
    assert (row["p"], row["p_low"], row["p_high"], row["q"]) == (0.8, 0.7, 0.9, 0.5)
    assert row["reason"] == "coin flip" and row["triage_kind"] == "not_researchable"
    assert row["manipulation_risk"] == "low" and row["market_id"] == mid
    rec = vault.read_filter_log(mid)
    assert rec["triage"]["triage_shadow"] == "done" and rec["status"] == "retired"
    assert not rec["passed"] and not world.of("briefer")
    assert vault.read_active_research(mid) is None
    assert not _rows(vault, "research_skip") and not _rows(vault, "traded")
    phases.phase3_qualitative_pipeline(vault, runner=world.runner)  # done: never repeated
    assert len(_rows(vault, "triage_shadow")) == 1


def test_high_manipulation_reason_and_cap_per_tick(world, vault):
    ids = _ids(True, TRIAGE_SHADOW_MAX_PER_TICK + 2)
    for mid in ids:
        world.market(mid, 0.5)
        _retired(vault, mid, risk="high")
    phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    rows = _rows(vault, "triage_shadow")
    assert len(rows) == TRIAGE_SHADOW_MAX_PER_TICK
    assert {r["reason"] for r in rows} == {"manipulation_risk"}
    assert {r["triage_kind"] for r in rows} == {"manipulation_risk"}


def test_never_displaces_real_quick_work(world, vault, monkeypatch):
    monkeypatch.setenv("OPENCLAW_QUICK_RESEARCH_BUDGET", "1")
    mid = _ids(True, 1)[0]
    world.market(mid, 0.5)
    _retired(vault, mid)
    world.market("real", 0.42)
    from tests.orchestrator.test_research_redesign import _seed
    _seed(vault, "real")
    world.dr_p = (0.6, 0.52, 0.68)
    out = phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    assert [r["market_id"] for r in out] == ["real"]
    assert len(world.of("deep_researcher", "quick")) == 2  # real quick first, then the shadow
    assert world.of("deep_researcher", "quick")[0]["market_id"] == "real"


def test_non_pending_retired_stays_ignored(world, vault):
    a, b = _ids(True, 2)
    for mid, pend in ((a, False), (b, None)):
        world.market(mid, 0.5)
        _retired(vault, mid, pending=pend)
    phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    assert not world.calls and not _rows(vault, "triage_shadow")


def test_content_error_ledgered_marked_done_never_inactive(world, vault):
    mid = _ids(True, 1)[0]
    world.market(mid, 0.5)
    _retired(vault, mid)

    def boom(role, payload):
        return {"error": "model refused"}

    phases.phase3_qualitative_pipeline(vault, runner=boom)
    (row,) = _rows(vault, "triage_shadow")
    assert row["error"] and "p" not in row
    rec = vault.read_filter_log(mid)
    assert rec["triage"]["triage_shadow"] == "done"
    assert rec.get("status") == "retired" and not rec.get("error_log")


def test_stats_triage_shadow_brier_split_and_excluded_from_main():
    rows = []
    for mid, kind, y, p, q in (("a", "not_researchable", 1, 0.8, 0.5), ("b", "manipulation_risk", 0, 0.4, 0.5)):
        rows += [{"stage": "triage_shadow", "market_id": mid, "triage_kind": kind, "p": p, "q": q},
                 {"stage": "triage_reject", "market_id": mid, "q": q},
                 {"stage": "resolution", "market_id": mid, "outcome_yes": y}]
    rows.append({"stage": "triage_shadow", "market_id": "e", "error": "x", "q": 0.5})
    rows.append({"stage": "shadow_failed", "market_id": "a", "p": 0.1, "q": 0.1})
    ts = stats.triage_shadow_stats(rows)
    assert ts["not_researchable"] == (1, pytest.approx(0.04), pytest.approx(0.25))
    assert ts["manipulation_risk"] == (1, pytest.approx(0.16), pytest.approx(0.25))
    assert ts["all"][0] == 2
    s = stats.compute_stats(rows)
    assert s["n_scored"] == 0 and "brier_p" not in s and s["lambda"] is None
    assert "Triage shadow" in stats.render_stats(s)
