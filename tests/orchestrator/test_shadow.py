"""Quick-stage shadow test: selection, queue ordering/cap, no side effects, ledger row, Brier."""

from __future__ import annotations

import pytest

from config.trading_constants import SHADOW_MAX_PER_TICK, SHADOW_RATE
from orchestrator import evidence, ledger, phases, stats
from tests.orchestrator.test_research_redesign import World, _seed


@pytest.fixture()
def world(monkeypatch, vault):
    return World(monkeypatch, vault)


def _ids(want: bool, n: int) -> list[str]:
    out = [m for m in (f"s{i}" for i in range(2000)) if evidence.is_shadow(m) is want]
    return out[:n]


def _quick_flat_full_far(payload):
    """Quick: q inside a narrow range (no_divergence); FULL: a clear, agreeing estimate."""
    return (0.5, 0.42, 0.58) if payload.get("mode") == "quick" else (0.7, 0.62, 0.78)


def _shadow_rows(vault):
    return [r for r in ledger.iter_rows(vault) if r["stage"] == "shadow_research"]


def test_is_shadow_rate_and_independent_of_ab_arm():
    ids = [f"0x{i:05x}" for i in range(6000)]
    flags = [evidence.is_shadow(m) for m in ids]
    assert flags == [evidence.is_shadow(m) for m in ids]
    assert abs(sum(flags) / len(ids) - SHADOW_RATE) < 0.03
    for arm in (evidence.AIQ, evidence.WEB):
        sub = [f for m, f in zip(ids, flags) if evidence.ab_arm(m) == arm]
        assert abs(sum(sub) / len(sub) - SHADOW_RATE) < 0.04


def test_shadow_run_has_no_report_no_state_change_and_one_ledger_row(world, vault):
    (mid,) = _ids(True, 1)
    world.market(mid, 0.5)
    _seed(vault, mid)
    world.dr_p = _quick_flat_full_far
    out = phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    assert out == []  # nothing reaches Phase 4
    assert vault.read_active_research(mid) is None
    state = vault.read_filter_log(mid)["research_state"]
    assert (state["stage"], state["outcome"]) == ("quick", "no_divergence")
    assert len(world.of("deep_researcher", "full")) == 3  # the FULL stage really ran
    (skip,) = [r for r in ledger.iter_rows(vault) if r["stage"] == "research_skip"]
    assert skip["reason"] == "no_divergence"
    (row,) = _shadow_rows(vault)
    assert row["shadow"] is True and row["would_skip"] is None
    assert (row["q"], row["p"], row["p_low"], row["p_high"]) == (0.5, 0.7, 0.62, 0.78)
    assert (row["quick_p"], row["quick_p_low"], row["quick_p_high"]) == (0.5, 0.42, 0.58)
    assert row["spread"] == 0 and row["evidence_source"] in ("aiq", "web")
    assert row["market_id"] == mid


def test_non_shadow_quick_skip_runs_no_full_stage(world, vault):
    (mid,) = _ids(False, 1)
    world.market(mid, 0.5)
    _seed(vault, mid)
    world.dr_p = _quick_flat_full_far
    assert phases.phase3_qualitative_pipeline(vault, runner=world.runner) == []
    assert not world.of("briefer") and not _shadow_rows(vault)


def test_shadow_jobs_never_displace_escalations_and_are_capped(world, vault, monkeypatch):
    monkeypatch.setenv("PMBOT_FULL_RESEARCH_BUDGET", "1")
    shadows = _ids(True, SHADOW_MAX_PER_TICK + 2)
    for i, mid in enumerate(shadows):  # scanned first (higher score)
        world.market(mid, 0.5)
        _seed(vault, mid, score=5.0 - i * 0.1)
    world.market("real", 0.42)
    _seed(vault, "real", score=0.1)

    def dr(payload):
        if payload["market_id"] == "real":
            return (0.6, 0.52, 0.68)
        return _quick_flat_full_far(payload)

    world.dr_p = dr
    out = phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    assert [r["market_id"] for r in out] == ["real"]  # budget of 1 went to the real escalation
    assert vault.read_active_research("real") is not None
    rows = _shadow_rows(vault)
    assert len(rows) == SHADOW_MAX_PER_TICK
    assert {r["market_id"] for r in rows} <= set(shadows)


def test_shadow_respects_tick_budget(world, vault, monkeypatch):
    (mid,) = _ids(True, 1)
    world.market(mid, 0.5)
    _seed(vault, mid)
    world.dr_p = _quick_flat_full_far
    monkeypatch.setenv("PMBOT_RESEARCH_TICK_BUDGET_SEC", "0")
    assert phases.phase3_qualitative_pipeline(vault, runner=world.runner) == []
    assert not _shadow_rows(vault)


# --- stats ---------------------------------------------------------------------


def _rows():
    rows = []
    for mid, y, quick_p, p, q in (("a", 1, 0.8, 0.9, 0.5), ("b", 0, 0.4, 0.2, 0.5)):
        rows.append({"stage": "shadow_research", "market_id": mid, "shadow": True,
                     "quick_p": quick_p, "p": p, "q": q})
        rows.append({"stage": "resolution", "market_id": mid, "outcome_yes": y})
    rows.append({"stage": "shadow_research", "market_id": "open", "quick_p": 0.5, "p": 0.5, "q": 0.5})
    return rows


def test_brier_shadow_math_and_unresolved_ignored():
    n, bq, bf, bm = stats.brier_shadow(_rows())
    assert n == 2
    assert bq == pytest.approx(((0.2) ** 2 + (0.4) ** 2) / 2)
    assert bf == pytest.approx(((0.1) ** 2 + (0.2) ** 2) / 2)
    assert bm == pytest.approx(0.25)
    assert stats.brier_shadow([]) == (0, None, None, None)


def test_main_brier_excludes_shadow_rows():
    rows = _rows()
    s = stats.compute_stats(rows)
    assert s["n_scored"] == 0 and "brier_p" not in s
    assert s["brier_shadow"][0] == 2
    assert stats.brier_by_evidence_source(rows) == {}
    # a real decision row for the same market still scores, ignoring the later shadow row
    rows.insert(2, {"stage": "traded", "market_id": "a", "p": 0.6, "q": 0.5})
    s = stats.compute_stats(rows)
    assert s["n_scored"] == 1 and s["brier_p"] == pytest.approx(0.16)
    assert "Shadow: quick vs full vs market" in stats.render_stats(s)
