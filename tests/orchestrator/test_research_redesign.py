"""Wave 3: price-blind two-stage research (QUICK -> Hub decision -> FULL), refresh, A-IQ cache."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from obsidian_utils import ObsidianManager
from orchestrator import config as orch_config
from orchestrator.phases import p3_evidence, p3_queue
from orchestrator import ledger, phases, scraper
from orchestrator.agent_outputs import BrieferOutput, DeepResearcherComplete
from orchestrator.aiq_bundle import fetch_research_bundle_cached, normalize_query
from orchestrator.scraper import MarketRow
from tests.orchestrator.test_phase3_helpers import (
    briefer_ok,
    deep_researcher_complete,
)

Q_PRICE_FIELDS = {"q", "yes_price", "no_price", "last_trade_price", "midpoint", "spread"}


@pytest.fixture()
def vault(tmp_path):
    v = ObsidianManager(vault_base=tmp_path)
    v.cold_start_protocol()
    return v


def _seed(vault, mid: str, score: float = 1.0, **extra: Any) -> None:
    vault.write_filter_log(
        mid,
        {
            "market_id": mid,
            "passed": True,
            "trigger": None,
            "confidence_multiplier": score,
            "score": score,
            "details": "ok",
            "error": None,
            **extra,
        },
    )


class World:
    """Fake scraper + A-IQ + agent runner that records every payload."""

    def __init__(self, monkeypatch, vault):
        self.vault = vault
        self.rows: dict[str, MarketRow] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fetches: list[list[str]] = []
        self.dr_p: Any = 0.6  # float, or callable(payload) -> (p, low, high)
        self._lock = threading.Lock()
        monkeypatch.setattr(scraper, "fetch_market_row", lambda mid: self.rows.get(mid))
        monkeypatch.setattr(p3_evidence, "fetch_research_bundle", self._fetch)

    def market(self, mid: str, q: float, event_id: str | None = None, **md: Any) -> None:
        data = {
            "yes_price": q, "no_price": round(1 - q, 4), "volume": 1_000.0,
            "liquidity": 20_000.0, "days_to_resolution": 30, **md,
        }
        if event_id:
            data["event_id"] = event_id
        self.rows[mid] = MarketRow(
            market_id=mid, market_title=f"Title {mid}", market_description="desc",
            market_data=data,
        )

    def _fetch(self, queries):
        with self._lock:
            self.fetches.append(list(queries))
        return [{"query": q, "research_data": f"data:{q}", "error": None} for q in queries]

    def runner(self, role: str, payload: dict[str, Any]) -> Any:
        with self._lock:
            self.calls.append((role, json.loads(json.dumps(payload))))
        if role == "briefer":
            return briefer_ok(payload)
        assert role == "deep_researcher", role
        p = self.dr_p(payload) if callable(self.dr_p) else self.dr_p
        if not isinstance(p, tuple):
            p = (p, None, None)
        return deep_researcher_complete(payload, estimated_p=p[0], p_low=p[1], p_high=p[2])

    def of(self, role: str, mode: str | None = None) -> list[dict[str, Any]]:
        return [
            pl for r, pl in self.calls
            if r == role and (mode is None or pl.get("mode") == mode)
        ]


@pytest.fixture()
def world(monkeypatch, vault):
    return World(monkeypatch, vault)


def _skips(vault, mid=None):
    return [
        r for r in ledger.iter_rows(vault)
        if r["stage"] == "research_skip" and (mid is None or r["market_id"] == mid)
    ]


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


# --- agent contracts --------------------------------------------------------


def test_models_never_see_q(world, vault):
    world.market("m1", 0.4137)
    _seed(vault, "m1")
    world.dr_p = 0.6
    out = phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    assert len(out) == 1
    seen = [p for r, p in world.calls]
    assert world.of("briefer") and world.of("deep_researcher", "quick") and world.of("deep_researcher", "full")
    for payload in seen:
        assert not Q_PRICE_FIELDS & set(_keys(payload))
        assert "0.4137" not in json.dumps(payload)
    # the briefer payload is exactly the text fields
    assert set(world.of("briefer")[0]) == {
        "market_id", "title", "description", "resolution_source", "end_date", "category", "today",
    }


def test_briefer_requires_exactly_four_queries():
    base = {"market_id": "m", "error": None}
    for n in (0, 3, 5):
        with pytest.raises(ValidationError):
            BrieferOutput.model_validate({**base, "research_queries": [f"q{i}" for i in range(n)]})
    assert BrieferOutput.model_validate({**base, "research_queries": list("abcd")})
    # an explicit error may carry no queries
    assert BrieferOutput.model_validate({"market_id": "m", "research_queries": [], "error": "x"})


def test_briefer_queries_always_carry_the_date(world, vault):
    world.market("m1", 0.42)
    _seed(vault, "m1")

    def runner(role, payload):
        if role == "briefer":
            return {"market_id": "m1", "research_queries": ["a", "b", "c", "d"], "error": None}
        return world.runner(role, payload)

    phases.phase3_qualitative_pipeline(vault, runner=runner)
    today = world.of("deep_researcher")[0]["today"]
    assert world.fetches and all(f"as of {today}" in q for q in world.fetches[0])


@pytest.mark.parametrize(
    "extra",
    [
        {"p_low": 0.7},  # p_low > p
        {"p_high": 0.4},  # p_high < p
        {"p_high": 1.2},
        {"confidence": "certain"},
    ],
)
def test_deep_researcher_complete_validation(extra):
    good = {
        "status": "complete", "market_id": "m", "estimated_p": 0.5, "p_low": 0.4, "p_high": 0.6,
        "confidence": "high", "resolution_checklist": ["a"], "key_assumptions": ["b"],
        "as_of": "2026-10-22", "markdown": "x",
    }
    assert DeepResearcherComplete.model_validate(good).as_of.isoformat() == "2026-10-22"
    with pytest.raises(ValidationError):
        DeepResearcherComplete.model_validate({**good, **extra})


# --- QUICK stage ------------------------------------------------------------


def test_quick_no_divergence_skips_and_is_ledgered(world, vault):
    world.market("m1", 0.5)
    _seed(vault, "m1")
    world.dr_p = (0.5, 0.42, 0.58)
    out = phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    assert out == []
    quick = world.of("deep_researcher", "quick")
    assert len(quick) == 1 and quick[0]["research_bundle"] == []
    assert not world.of("briefer") and not world.fetches
    (skip,) = _skips(vault, "m1")
    assert skip["reason"] == "no_divergence" and skip["research_stage"] == "quick"
    assert (skip["p"], skip["p_low"], skip["p_high"], skip["q"]) == (0.5, 0.42, 0.58, 0.5)
    assert vault.read_filter_log("m1")["research_state"]["outcome"] == "no_divergence"

    # cooldown: the same, unmoved market is not re-researched next tick...
    world.calls.clear()
    phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    assert world.calls == []
    # ...but a > 8 point price move invalidates it.
    rec = vault.read_filter_log("m1")
    vault.write_filter_log("m1", {**rec, "last_q": 0.62})
    world.market("m1", 0.62)
    phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    assert len(world.of("deep_researcher", "quick")) == 1


def test_quick_q_outside_range_escalates(world, vault):
    world.market("m1", 0.42)
    _seed(vault, "m1")
    world.dr_p = (0.6, 0.52, 0.68)
    out = phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    assert len(out) == 1 and world.of("briefer")
    assert vault.read_filter_log("m1")["research_state"]["outcome"] == "researched"


def test_quick_wide_range_escalates_even_if_it_contains_q(world, vault):
    assert phases.quick_decision(0.2, 0.8, 0.5) == "escalate"
    assert phases.quick_decision(0.4, 0.7, 0.5) == "no_divergence"  # width exactly 0.3
    assert phases.quick_decision(0.4, 0.5, 0.52) == "escalate"
    world.market("m1", 0.5)
    _seed(vault, "m1")
    world.dr_p = (0.5, 0.2, 0.8)
    out = phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    assert len(out) == 1 and not _skips(vault)


def test_escalation_survives_full_budget_exhaustion(world, vault, monkeypatch):
    monkeypatch.setenv("OPENCLAW_FULL_RESEARCH_BUDGET", "1")
    for i, mid in enumerate(("a", "b")):
        world.market(mid, 0.42)
        _seed(vault, mid, score=2.0 - i)
    out = phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    assert [r["market_id"] for r in out] == ["a"]
    assert len(world.of("deep_researcher", "quick")) == 2
    assert vault.read_filter_log("b")["research_state"]["outcome"] == "escalate"
    world.calls.clear()
    out = phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    # next tick: b goes straight to FULL without a second quick call
    assert [r["market_id"] for r in out] == ["b"]
    assert world.of("deep_researcher", "quick") == []


# --- FULL stage -------------------------------------------------------------


def test_full_stage_writes_median_and_runs_three_independent_samples(world, vault):
    world.market("m1", 0.42)
    _seed(vault, "m1")
    by_tag = {
        None: (0.6, 0.5, 0.7),  # quick
        "s1": (0.55, 0.45, 0.60),
        "s2": (0.60, 0.50, 0.70),
        "s3": (0.65, 0.40, 0.80),
    }
    world.dr_p = lambda payload: by_tag.get(
        None if payload["mode"] == "quick" else payload["session_tag"]
    )
    (row,) = phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    full = world.of("deep_researcher", "full")
    assert [p["session_tag"] for p in full] == ["s1", "s2", "s3"]
    assert all(p["research_bundle"] for p in full)
    # median of each: p 0.60, low 0.45, high 0.70 (min/max would be 0.40 / 0.80)
    assert (row["p_value"], row["p_low"], row["p_high"]) == (0.60, 0.45, 0.70)
    fm = vault.read_market_record("m1", "active")
    assert fm["estimated_p"] == 0.60 and fm["p_low"] == 0.45 and fm["p_high"] == 0.70
    assert fm["q_at_research"] == 0.42 and fm["samples"] == [0.55, 0.60, 0.65]
    assert fm["confidence"] == "medium" and fm["as_of"] and fm["researched_at"]
    assert fm["resolution_checklist"] and fm["key_assumptions"]


def test_aggregate_samples_median_and_conservative_confidence():
    def comp(p, lo, hi, conf):
        return (
            DeepResearcherComplete(
                status="complete", market_id="m", estimated_p=p, p_low=lo, p_high=hi,
                confidence=conf, resolution_checklist=[], key_assumptions=[],
                as_of="2026-10-22", markdown="x",
            ),
            None,
        )

    agg = phases.aggregate_samples(
        [comp(0.5, 0.4, 0.55, "high"), comp(0.7, 0.6, 0.9, "low"), comp(0.55, 0.3, 0.6, "medium")]
    )
    assert (agg["p"], agg["p_low"], agg["p_high"]) == (0.55, 0.4, 0.6)
    assert agg["chosen"] == 2 and agg["confidence"] == "low"
    assert agg["spread"] == pytest.approx(0.2)
    # two samples: the mean of the pair
    two = phases.aggregate_samples([comp(0.4, 0.3, 0.5, "high"), comp(0.5, 0.4, 0.6, "high")])
    assert two["p"] == pytest.approx(0.45) and two["p_low"] <= two["p"] <= two["p_high"]


def test_sample_disagreement_is_skipped(world, vault):
    world.market("m1", 0.42)
    _seed(vault, "m1")
    ps = {"s1": 0.5, "s2": 0.6, "s3": 0.75}
    world.dr_p = lambda payload: (0.6, 0.5, 0.7) if payload["mode"] == "quick" else (
        ps[payload["session_tag"]], None, None
    )
    out = phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    assert out == [] and not vault.active_research_path("m1").exists()
    (skip,) = _skips(vault, "m1")
    assert skip["reason"] == "sample_disagreement" and skip["research_stage"] == "full"
    assert skip["samples"] == [0.5, 0.6, 0.75] and skip["spread"] == pytest.approx(0.25)
    assert vault.read_filter_log("m1")["research_state"]["outcome"] == "sample_disagreement"


def test_probable_misread_is_skipped_with_all_values(world, vault):
    world.market("m1", 0.2)
    _seed(vault, "m1")
    world.dr_p = 0.65  # |p - q| = 0.45 > 0.40
    out = phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    assert out == [] and not vault.active_research_path("m1").exists()
    (skip,) = _skips(vault, "m1")
    assert skip["reason"] == "probable_misread"
    assert skip["q"] == 0.2 and skip["p"] == 0.65
    assert skip["p_low"] == pytest.approx(0.57) and skip["p_high"] == pytest.approx(0.73)
    assert skip["samples"] == [0.65, 0.65, 0.65] and skip["confidence"] == "medium"
    # not retried next tick while q is unchanged
    world.calls.clear()
    phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    assert world.calls == []


def test_gap_exactly_at_threshold_is_not_a_misread(world, vault):
    world.market("m1", 0.25)
    _seed(vault, "m1")
    world.dr_p = 0.5  # gap 0.25
    assert len(phases.phase3_qualitative_pipeline(vault, runner=world.runner)) == 1


def test_full_stage_follow_up_cap_then_forced_synthesis(world, vault):
    world.market("m1", 0.42)
    _seed(vault, "m1")
    seen: list[tuple[str, bool]] = []

    def runner(role, payload):
        if role != "deep_researcher" or payload["mode"] == "quick" or payload.get("system_override"):
            return world.runner(role, payload)
        seen.append((payload["session_tag"], bool(payload.get("system_override"))))
        return {"status": "needs_more_data", "new_queries": ["more as of today"]}

    out = phases.phase3_qualitative_pipeline(vault, runner=runner)
    assert len(out) == 1
    assert len(seen) == 2  # 1 round + 1 follow-up, then forced synthesis (and no 3rd round)


# --- budgets, concurrency, event reuse -------------------------------------


def test_quick_and_full_budgets_are_enforced_in_score_order(world, vault, monkeypatch):
    monkeypatch.setenv("OPENCLAW_QUICK_RESEARCH_BUDGET", "3")
    monkeypatch.setenv("OPENCLAW_FULL_RESEARCH_BUDGET", "2")
    for i in range(5):
        mid = f"m{i}"
        world.market(mid, 0.42)
        _seed(vault, mid, score=5.0 - i)
    out = phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    quick_ids = [p["market_id"] for p in world.of("deep_researcher", "quick")]
    assert quick_ids == ["m0", "m1", "m2"]
    assert sorted(r["market_id"] for r in out) == ["m0", "m1"]
    assert {p["market_id"] for p in world.of("briefer")} == {"m0", "m1"}
    assert not vault.active_research_path("m2").exists()


def test_tick_wall_clock_budget_stops_new_markets(world, vault, monkeypatch):
    monkeypatch.setenv("OPENCLAW_RESEARCH_TICK_BUDGET_SEC", "0")
    world.market("m1", 0.42)
    _seed(vault, "m1")
    assert phases.phase3_qualitative_pipeline(vault, runner=world.runner) == []
    assert world.calls == []

    monkeypatch.setenv("OPENCLAW_RESEARCH_TICK_BUDGET_SEC", "1800")
    assert len(phases.phase3_qualitative_pipeline(vault, runner=world.runner)) == 1


def test_tick_budget_checked_between_full_markets(world, vault, monkeypatch):
    """The QUICK scan fits the budget, but FULL markets queued behind a slow one do not start."""
    monkeypatch.setenv("OPENCLAW_RESEARCH_WORKERS", "1")
    clock = {"t": 0.0}
    monkeypatch.setattr(p3_queue.time, "monotonic", lambda: clock["t"])
    monkeypatch.setenv("OPENCLAW_RESEARCH_TICK_BUDGET_SEC", "100")
    for i, mid in enumerate(("a", "b")):
        world.market(mid, 0.42)
        _seed(vault, mid, score=2.0 - i)

    def runner(role, payload):
        if role == "briefer":
            clock["t"] += 500  # the first FULL market blows the budget
        return world.runner(role, payload)

    out = phases.phase3_qualitative_pipeline(vault, runner=runner)
    assert [r["market_id"] for r in out] == ["a"]


def test_full_stage_markets_run_concurrently(world, vault, monkeypatch):
    monkeypatch.setenv("OPENCLAW_RESEARCH_WORKERS", "2")
    for mid in ("a", "b"):
        world.market(mid, 0.42, event_id=f"ev-{mid}")
        _seed(vault, mid)
    barrier = threading.Barrier(2, timeout=10)

    def runner(role, payload):
        if role == "briefer":
            barrier.wait()  # only passes if both markets are in the briefer at once
        return world.runner(role, payload)

    out = phases.phase3_qualitative_pipeline(vault, runner=runner)
    assert sorted(r["market_id"] for r in out) == ["a", "b"]
    assert {r["market_id"] for r in ledger.iter_rows(vault)} <= {"a", "b"}


def test_same_event_markets_share_one_bundle(world, vault):
    for mid in ("a", "b"):
        world.market(mid, 0.42, event_id="ev-1")
        _seed(vault, mid)
    out = phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    assert sorted(r["market_id"] for r in out) == ["a", "b"]
    assert len(world.of("briefer")) == 1 and len(world.fetches) == 1
    full = world.of("deep_researcher", "full")
    assert sorted({p["market_id"] for p in full}) == ["a", "b"]
    assert len(full) == 6  # the Deep Researcher still runs per market (3 samples each)
    assert vault.read_research_bundle("a") == vault.read_research_bundle("b")


def test_infra_failure_in_a_worker_aborts_the_tick(world, vault):
    from orchestrator.openclaw_cli import OpenClawCLIError
    from orchestrator.state import InfraUnavailable

    world.market("a", 0.42)
    _seed(vault, "a")

    def runner(role, payload):
        if role == "briefer":
            raise OpenClawCLIError("gateway down")
        return world.runner(role, payload)

    with pytest.raises(InfraUnavailable):
        phases.phase3_qualitative_pipeline(vault, runner=runner)
    assert not vault.is_market_inactive("a")


# --- A-IQ cache -------------------------------------------------------------


def test_aiq_cache_hit_expiry_and_error_skip(tmp_path):
    path = tmp_path / "aiq_cache.json"
    fetched: list[list[str]] = []

    def fetch(qs):
        fetched.append(list(qs))
        return [
            {"query": q, "research_data": "" if "bad" in q else f"d:{q}",
             "error": "boom" if "bad" in q else None}
            for q in qs
        ]

    assert normalize_query("  Who   WINS?\n") == "who wins?"
    r1 = fetch_research_bundle_cached(["Who wins?", "bad one"], path, fetch=fetch, now=1000.0)
    assert [r["query"] for r in r1] == ["Who wins?", "bad one"] and r1[1]["error"] == "boom"
    # normalised hit (different case / whitespace), keeps the caller's text; errors are retried
    r2 = fetch_research_bundle_cached(["  who   WINS? ", "bad one"], path, fetch=fetch, now=1000.0 + 3600)
    assert fetched == [["Who wins?", "bad one"], ["bad one"]]
    assert r2[0]["query"] == "  who   WINS? " and r2[0]["research_data"] == "d:Who wins?"
    # just before / after 24h
    fetch_research_bundle_cached(["who wins?"], path, fetch=fetch, now=1000.0 + 86399)
    assert len(fetched) == 2
    fetch_research_bundle_cached(["who wins?"], path, fetch=fetch, now=1000.0 + 86401)
    assert fetched[-1] == ["Who wins?".lower()]
    data = json.loads(path.read_text())
    assert list(data) == ["who wins?"]  # expired + error entries pruned/never stored


def test_phase3_uses_vault_cache_across_markets(world, vault):
    for mid in ("a", "b"):
        world.market(mid, 0.42, event_id=f"ev-{mid}")
        _seed(vault, mid)

    def runner(role, payload):
        if role == "briefer":  # identical (normalised) queries for both markets
            return {
                "market_id": payload["market_id"],
                "research_queries": [f"Shared  Query {i} as of {payload['today']}" for i in range(4)],
                "error": None,
            }
        return world.runner(role, payload)

    phases.phase3_qualitative_pipeline(vault, runner=runner)
    assert (vault._dirs["active"] / "aiq_cache.json").exists()
    assert sum(len(f) for f in world.fetches) == 4  # second market served from the cache


# --- refresh ----------------------------------------------------------------


def _research(vault, mid, *, q=0.42, hours_ago=1.0, days=30, **extra):
    at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    vault.write_research_report(
        mid,
        {
            "market_id": mid, "estimated_p": 0.6, "p_low": 0.5, "p_high": 0.7,
            "q_at_research": q, "researched_at": at.isoformat(),
            "days_at_research": days, **extra,
        },
        "## Bull Thesis\n\nb\n\n## Bear Thesis\n\nb\n\n## Post-Mortem\n",
    )


def test_research_ttl_formula():
    assert phases.research_ttl_hours(5) == 48
    assert phases.research_ttl_hours(20) == 48
    assert phases.research_ttl_hours(30) == 72
    assert phases.research_ttl_hours(60) == 144
    assert phases.research_ttl_hours(None) == 48


def test_refresh_ttl_trigger(vault):
    _seed(vault, "fresh")
    _research(vault, "fresh", hours_ago=71)  # 30 days left -> TTL 72h
    _seed(vault, "stale")
    _research(vault, "stale", hours_ago=73)
    _seed(vault, "short")
    _research(vault, "short", hours_ago=49, days=5)  # TTL floor 48h
    queue = {c.market_id: c.kind for c in phases.build_phase3_queue(vault)}
    assert queue == {"stale": "refresh", "short": "refresh"}


def test_refresh_skips_open_bets(vault):
    _seed(vault, "bet")
    _research(vault, "bet", hours_ago=200)
    vault.write_trade_log("bet", {"market_id": "bet", "allocation_usd": 10.0, "executed": False})
    assert phases.build_phase3_queue(vault) == []


def test_phase2_flags_price_move_refresh_and_phase3_runs_full_only(world, vault):
    _seed(vault, "m1", last_q=0.42)
    _research(vault, "m1", q=0.30, hours_ago=1.0)
    world.market("m1", 0.42)  # +12 points since research
    phases.phase2_quantitative_routing(vault, [world.rows["m1"]], runner=lambda r, p: pytest.fail(r))
    assert vault.read_filter_log("m1")["refresh_reason"] == "price_move"
    (cand,) = phases.build_phase3_queue(vault)
    assert (cand.market_id, cand.kind) == ("m1", "refresh")

    world.dr_p = 0.6
    (row,) = phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    assert world.of("deep_researcher", "quick") == []  # refresh skips the QUICK stage
    assert world.of("briefer") and len(world.of("deep_researcher", "full")) == 3
    assert vault.read_market_record("m1", "active")["q_at_research"] == 0.42
    rec = vault.read_filter_log("m1")
    assert rec["refresh_reason"] is None and rec["research_state"]["outcome"] == "researched"
    assert phases.build_phase3_queue(vault) == []


def test_small_price_move_does_not_refresh(world, vault):
    _seed(vault, "m1", last_q=0.37)
    _research(vault, "m1", q=0.30, hours_ago=1.0)
    world.market("m1", 0.37)  # +7 points
    phases.phase2_quantitative_routing(vault, [world.rows["m1"]], runner=lambda r, p: pytest.fail(r))
    assert vault.read_filter_log("m1").get("refresh_reason") is None
    assert phases.build_phase3_queue(vault) == []


def test_refresh_counts_against_full_budget(world, vault, monkeypatch):
    monkeypatch.setenv("OPENCLAW_FULL_RESEARCH_BUDGET", "1")
    _seed(vault, "old", score=2.0)
    _research(vault, "old", hours_ago=500)
    world.market("old", 0.42)
    _seed(vault, "new", score=1.0)
    world.market("new", 0.42)
    out = phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    assert [r["market_id"] for r in out] == ["old"]  # score order; "new" waits (escalated)
    assert vault.read_filter_log("new")["research_state"]["outcome"] == "escalate"


def test_failed_refresh_does_not_loop(world, vault):
    _seed(vault, "m1")
    _research(vault, "m1", hours_ago=500, q=0.42)
    world.market("m1", 0.42)
    world.dr_p = lambda payload: (0.5, None, None) if payload["session_tag"] == "s1" else (0.8, None, None)
    assert phases.phase3_qualitative_pipeline(vault, runner=world.runner) == []  # disagreement
    assert _skips(vault, "m1")[0]["reason"] == "sample_disagreement"
    assert phases.build_phase3_queue(vault) == []  # cooldown until its TTL / a price move
    assert vault.read_market_record("m1", "active")["estimated_p"] == 0.6  # old research kept


def test_edge_refresh_machinery_removed():
    assert not hasattr(phases, "_edge_refresh_research")
    assert not hasattr(orch_config, "max_edge_research_refreshes")
    fm = __import__("obsidian_utils").ResearchFrontmatter(market_id="m", estimated_p=0.5)
    assert "edge_research_refresh_count" not in fm.model_dump()
