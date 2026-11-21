"""Phase 2 end to end: gate -> rank -> triage, budgets, caching, ledger."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from orchestrator import ledger, phases, polymarket_api
from orchestrator.gate import MAX_FAILS
from orchestrator.phases import common, p3_queue
from orchestrator.scraper import MarketRow


@pytest.fixture(autouse=True)
def _no_holder_network(monkeypatch):
    monkeypatch.setattr(polymarket_api, "fetch_holder_concentration", lambda cid: None)


def mk(mid: str, *, vol: float = 1_000.0, event_id: str | None = None, **over: Any) -> MarketRow:
    md = {
        "yes_price": 0.4,
        "no_price": 0.6,
        "volume": vol,
        "liquidity": 20_000.0,
        "days_to_resolution": 30,
        "category": "politics",
    }
    if event_id:
        md["event_id"] = event_id
    md.update(over)
    return MarketRow(market_id=mid, market_title=f"Will {mid}?", market_data=md)


class Triage:
    def __init__(self, verdicts: dict[str, bool] | None = None, risk: Any = "low"):
        self.calls: list[dict[str, Any]] = []
        self.verdicts = verdicts or {}
        self.risk = risk

    def __call__(self, role: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert role == "triage"
        self.calls.append(payload)
        return {
            "market_id": payload["market_id"],
            "researchable": self.verdicts.get(payload["market_id"], True),
            "category": "politics",
            "reason": "r",
            "manipulation_risk": self.risk,
            "manipulation_reason": "mr",
            "error": None,
        }


def stages(vault, mid):
    return [r["stage"] for r in ledger.iter_rows(vault) if r["market_id"] == mid]


def test_triage_payload_has_no_price(vault):
    t = Triage()
    phases.phase2_quantitative_routing(vault, [mk("a")], runner=t)
    assert set(t.calls[0]) == {
        "market_id", "title", "description", "resolution_source", "category", "end_date", "today",
    }


def test_default_triages_and_promotes_every_market(vault):
    markets = [mk(f"m{i}", vol=1_000.0 * (i + 1) ** 2) for i in range(6)]
    t = Triage()
    phases.phase2_quantitative_routing(vault, markets, runner=t)
    ids = [f"m{i}" for i in range(6)]
    assert sorted(c["market_id"] for c in t.calls) == ids
    assert all(vault.read_filter_log(m)["passed"] for m in ids)
    assert sorted(c.market_id for c in phases.build_phase3_queue(vault)) == ids


def test_end_date_written_to_filter_record(vault):
    phases.phase2_quantitative_routing(
        vault, [mk("a", end_date="2026-12-01T00:00:00+00:00"), mk("b")], runner=Triage()
    )
    assert vault.read_filter_log("a")["end_date"] == "2026-12-01T00:00:00+00:00"
    assert vault.read_filter_log("b").get("end_date") is None


def test_integrity_fetch_concurrent_preserves_results(vault, monkeypatch):
    import threading

    threads: set[int] = set()

    def hist(token):
        threads.add(threading.get_ident())
        return None

    monkeypatch.setattr(polymarket_api, "fetch_price_history", hist)
    monkeypatch.setattr(
        polymarket_api, "fetch_holder_concentration",
        lambda cid: {"top1": 0.1, "top3": 0.2, "basis": "supply"},
    )
    markets = [mk(f"m{i}", yes_token_id=f"t{i}") for i in range(10)]
    t = Triage()
    phases.phase2_quantitative_routing(vault, markets, runner=t)
    assert sorted(c["market_id"] for c in t.calls) == sorted(f"m{i}" for i in range(10))
    for i in range(10):
        rec = vault.read_filter_log(f"m{i}")
        assert rec["passed"] is True
        assert rec["score"] == rec["confidence_multiplier"] > 0


def test_budgets_respected_and_ranked(vault, monkeypatch):
    monkeypatch.setenv("PMBOT_GATE_BUDGET", "4")
    monkeypatch.setenv("PMBOT_TRIAGE_BUDGET", "2")
    # lower volume => lower attention => higher score
    markets = [mk(f"m{i}", vol=1_000.0 * (i + 1) ** 2) for i in range(6)]
    t = Triage()
    phases.phase2_quantitative_routing(vault, markets, runner=t)
    assert sorted(c["market_id"] for c in t.calls) == ["m0", "m1", "m2", "m3"]
    passed = [m for m in ("m0", "m1", "m2", "m3", "m4", "m5") if vault.read_filter_log(m)["passed"]]
    assert passed == ["m0", "m1"]
    assert vault.read_filter_log("m3")["trigger"] == "over_triage_budget"
    assert vault.read_filter_log("m5")["trigger"] == "over_gate_budget"
    queue = [c.market_id for c in phases.build_phase3_queue(vault)]
    assert queue == ["m0", "m1"]
    rec = vault.read_filter_log("m0")
    assert rec["score"] == rec["confidence_multiplier"] > 0


def test_triage_once_per_event_and_cached_across_ticks(vault):
    t = Triage()
    markets = [mk("a", event_id="E1"), mk("b", event_id="E1"), mk("c", event_id="E2")]
    phases.phase2_quantitative_routing(vault, markets, runner=t)
    assert len(t.calls) == 2  # one for E1, one for E2
    assert vault.read_filter_log("b")["triage"]["researchable"] is True

    # next tick, markets due again (state is unchanged): no new triage calls
    t2 = Triage()
    phases.phase2_quantitative_routing(vault, markets, runner=t2)
    assert t2.calls == []
    # a new sibling of a known event reuses the verdict
    phases.phase2_quantitative_routing(vault, [mk("d", event_id="E1")], runner=t2)
    assert t2.calls == []
    assert vault.read_filter_log("d")["triage"]["researchable"] is True


def test_triage_reject_retires_and_ledgers(vault):
    t = Triage({"bad": False})
    phases.phase2_quantitative_routing(vault, [mk("bad"), mk("good")], runner=t)
    rec = vault.read_filter_log("bad")
    assert rec["status"] == "retired" and rec["passed"] is False
    assert rec["triage"]["researchable"] is False
    assert stages(vault, "bad") == ["triage_reject"]
    assert vault.read_filter_log("good")["passed"] is True

    # retired markets are skipped silently on later ticks
    t2 = Triage()
    phases.phase2_quantitative_routing(vault, [mk("bad")], runner=t2)
    assert t2.calls == [] and stages(vault, "bad") == ["triage_reject"]
    assert "bad" not in [c.market_id for c in phases.build_phase3_queue(vault)]


def test_event_reject_applies_to_siblings(vault):
    t = Triage({"a": False})
    phases.phase2_quantitative_routing(
        vault, [mk("a", event_id="E"), mk("b", event_id="E")], runner=t
    )
    assert len(t.calls) == 1
    assert vault.read_filter_log("b")["status"] == "retired"
    assert stages(vault, "b") == ["triage_reject"]


def test_gate_reject_ledgered_once_with_backoff(vault):
    far = mk("far", days_to_resolution=200)
    t = Triage()
    phases.phase2_quantitative_routing(vault, [far], runner=t)
    rec = vault.read_filter_log("far")
    assert rec["trigger"] == "too_far" and rec["fail_count"] == 1
    assert rec["next_check_at"] and rec["last_q"] == 0.4
    assert stages(vault, "far") == ["gate_reject"]
    assert t.calls == []

    # not due: nothing changes
    phases.phase2_quantitative_routing(vault, [far], runner=t)
    assert vault.read_filter_log("far")["fail_count"] == 1

    # make it due: fail again, still only one ledger row (no state change)
    vault.patch_frontmatter(
        "far", "filters",
        {"next_check_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()},
    )
    phases.phase2_quantitative_routing(vault, [far], runner=t)
    assert vault.read_filter_log("far")["fail_count"] == 2
    assert stages(vault, "far") == ["gate_reject"]


def test_retired_after_six_fails(vault):
    far = mk("far", days_to_resolution=200)
    for _ in range(MAX_FAILS):
        phases.phase2_quantitative_routing(vault, [far], runner=Triage())
        vault.patch_frontmatter(
            "far", "filters",
            {"next_check_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()},
        )
    rec = vault.read_filter_log("far")
    assert rec["status"] == "retired" and rec["fail_count"] == MAX_FAILS


def test_arbitrage_ledgered_not_researched(vault):
    t = Triage()
    phases.phase2_quantitative_routing(
        vault, [mk("arb", yes_price=0.45, no_price=0.5)], runner=t
    )
    assert t.calls == []
    assert stages(vault, "arb") == ["arb_opportunity"]
    phases.phase2_quantitative_routing(
        vault, [mk("arb", yes_price=0.45, no_price=0.5)], runner=t
    )
    assert stages(vault, "arb") == ["arb_opportunity"]  # deduped
    assert vault.read_filter_log("arb") is None


def test_triage_agent_error_flags_inactive(vault):
    def bad(role, payload):
        return {"market_id": payload["market_id"], "researchable": True,
                "category": "x", "reason": "r", "error": "boom"}

    phases.phase2_quantitative_routing(vault, [mk("a")], runner=bad)
    assert vault.is_market_inactive("a")




def test_holders_reject_backs_off_and_ledgers(vault, monkeypatch):
    monkeypatch.setattr(
        polymarket_api, "fetch_holder_concentration", lambda cid: {"top1": 0.9, "top3": 0.99, "basis": "supply"}
    )
    t = Triage()
    phases.phase2_quantitative_routing(vault, [mk("whale")], runner=t)
    rec = vault.read_filter_log("whale")
    assert rec["trigger"] == "concentrated" and rec["fail_count"] == 1 and rec["next_check_at"]
    assert stages(vault, "whale") == ["gate_reject"]
    assert t.calls == []


def test_holders_none_does_not_reject(vault):
    t = Triage()
    phases.phase2_quantitative_routing(vault, [mk("a")], runner=t)
    assert vault.read_filter_log("a")["passed"] is True and len(t.calls) == 1


def test_high_manipulation_risk_retires(vault):
    t = Triage(risk="high")
    phases.phase2_quantitative_routing(vault, [mk("m")], runner=t)
    rec = vault.read_filter_log("m")
    assert rec["status"] == "retired" and rec["passed"] is False
    rows = [r for r in ledger.iter_rows(vault) if r["market_id"] == "m"]
    assert [r["stage"] for r in rows] == ["triage_reject"]
    assert rows[0]["reason"] == "manipulation_risk" and rows[0]["manipulation_reason"] == "mr"
    assert rows[0]["manipulation_risk"] == "high"


def test_medium_manipulation_risk_passes_and_is_carried(vault):
    phases.phase2_quantitative_routing(vault, [mk("m")], runner=Triage(risk="medium"))
    rec = vault.read_filter_log("m")
    assert rec["passed"] is True and rec["triage"]["manipulation_risk"] == "medium"
    md = {"manipulation_risk": rec["triage"]["manipulation_risk"]}
    assert common._ledger_market_fields(md)["manipulation_risk"] == "medium"
    assert common._ledger_market_fields({})["manipulation_risk"] == "unknown"


def test_old_cache_without_field_is_unknown_no_agent_call(vault):
    phases.phase2_quantitative_routing(vault, [mk("a")], runner=Triage())
    vault.patch_frontmatter("a", "filters", {"triage": {"researchable": True, "category": "x", "reason": "r"}})
    vault.patch_frontmatter(
        "a", "filters",
        {"next_check_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()},
    )
    t = Triage()
    phases.phase2_quantitative_routing(vault, [mk("a")], runner=t)
    assert t.calls == []
    assert vault.read_filter_log("a")["passed"] is True


def test_bad_manipulation_enum_flags_inactive(vault):
    phases.phase2_quantitative_routing(vault, [mk("a")], runner=Triage(risk="extreme"))
    assert vault.is_market_inactive("a")


def _run_p3_p4(vault, monkeypatch, rows):
    # Phase 2 mutates its MarketRow objects; real Phase 3 re-fetches fresh rows, so mirror that.
    rows = {k: mk(k, **{x: v.market_data[x] for x in ("yes_price", "no_price")}) for k, v in rows.items()}
    from orchestrator import scraper
    from orchestrator.phases import p3_evidence

    monkeypatch.setenv("PMBOT_MODE", "stub")
    monkeypatch.setattr(scraper, "fetch_market_row", lambda mid: rows.get(mid))
    monkeypatch.setattr(scraper, "fetch_resolution", lambda mid: None)
    monkeypatch.setattr(
        p3_evidence, "fetch_research_bundle",
        lambda qs: [{"query": q, "research_data": "stub data", "error": None} for q in qs],
    )
    researched = phases.phase3_qualitative_pipeline(vault)
    phases.phase4_execution(vault, researched)


def _row_for(vault, mid):
    return {r["stage"]: r for r in ledger.iter_rows(vault) if r["market_id"] == mid}


def test_medium_risk_reaches_phase3_and_phase4_ledger_rows(vault, monkeypatch):
    rows = {"skip": mk("skip", yes_price=0.55, no_price=0.45), "buy": mk("buy", yes_price=0.26, no_price=0.74)}
    phases.phase2_quantitative_routing(vault, list(rows.values()), runner=Triage(risk="medium"))
    _run_p3_p4(vault, monkeypatch, rows)
    assert _row_for(vault, "skip")["research_skip"]["manipulation_risk"] == "medium"
    by = _row_for(vault, "buy")
    assert by["traded"]["manipulation_risk"] == "medium"


def test_phase4_traded_row_carries_risk_single_market(vault, monkeypatch):
    rows = {"buy": mk("buy", yes_price=0.26, no_price=0.74)}
    phases.phase2_quantitative_routing(vault, list(rows.values()), runner=Triage(risk="medium"))
    _run_p3_p4(vault, monkeypatch, rows)
    assert _row_for(vault, "buy")["traded"]["manipulation_risk"] == "medium"


def test_old_cache_without_field_stays_unknown_in_phase3(vault, monkeypatch):
    rows = {"skip": mk("skip", yes_price=0.55, no_price=0.45)}
    phases.phase2_quantitative_routing(vault, list(rows.values()), runner=Triage(risk="medium"))
    vault.patch_frontmatter("skip", "filters", {"triage": {"researchable": True, "category": "x", "reason": "r"}})
    _run_p3_p4(vault, monkeypatch, rows)
    assert _row_for(vault, "skip")["research_skip"]["manipulation_risk"] == "unknown"


def test_build_phase3_queue_sorts_and_caps(monkeypatch, vault):
    monkeypatch.setattr(p3_queue, "top_qualitative_markets", lambda: 2)

    mult_by_id = {"low": 1.0, "high": 5.0, "mid": 3.0}
    for mid, mult in mult_by_id.items():
        vault.write_filter_log(
            mid,
            {
                "market_id": mid,
                "passed": True,
                "trigger": "stub",
                "confidence_multiplier": mult,
                "details": "ok",
                "error": None,
            },
        )

    queue = phases.build_phase3_queue(vault)
    assert [c.market_id for c in queue] == ["high", "mid"]


def test_build_phase3_queue_orders_by_end_date(monkeypatch, vault):
    monkeypatch.setattr(p3_queue, "top_qualitative_markets", lambda: 10)
    spec = {
        "late": ("2026-12-30T00:00:00+00:00", 9.0),
        "soon_lo": ("2026-12-01T00:00:00+00:00", 1.0),
        "soon_hi": ("2026-12-01T00:00:00+00:00", 5.0),
        "none": (None, 99.0),
    }
    for mid, (end, mult) in spec.items():
        vault.write_filter_log(mid, {
            "market_id": mid, "passed": True, "trigger": "stub", "confidence_multiplier": mult,
            "details": "ok", "error": None, "end_date": end,
        })
    queue = phases.build_phase3_queue(vault)
    assert [c.market_id for c in queue] == ["soon_hi", "soon_lo", "late", "none"]


def test_build_phase3_queue_logs_skip_counter(vault, caplog):
    import logging

    vault.write_filter_log("a", {
        "market_id": "a", "passed": True, "trigger": "stub", "confidence_multiplier": 1.0,
        "details": "ok", "error": None,
    })
    with caplog.at_level(logging.INFO):
        phases.build_phase3_queue(vault)
    assert any(
        "[PHASE 3 queue] candidates=1 skip_fresh=0 skip_cooldown=0 skip_open_bet=0" in r.getMessage()
        for r in caplog.records
    )


def _triage_with(researchable):
    def run(role, payload):
        return {"market_id": payload["market_id"], "researchable": researchable, "category": "x",
                "reason": "r", "manipulation_risk": "low", "manipulation_reason": "mr", "error": None}
    return run


@pytest.mark.parametrize("value", ["false", "False", " FALSE "])
def test_triage_string_false_is_not_researchable(vault, value):
    phases.phase2_quantitative_routing(vault, [mk("a")], runner=_triage_with(value))
    assert vault.read_filter_log("a")["triage"]["researchable"] is False
    assert stages(vault, "a") == ["triage_reject"]


def test_triage_string_true_is_researchable(vault):
    phases.phase2_quantitative_routing(vault, [mk("a")], runner=_triage_with("true"))
    assert vault.read_filter_log("a")["passed"] is True


@pytest.mark.parametrize("value", ["<JSON boolean true or false>", "maybe", None, 1])
def test_triage_garbage_researchable_flags_inactive(vault, value):
    phases.phase2_quantitative_routing(vault, [mk("a")], runner=_triage_with(value))
    assert vault.is_market_inactive("a")
