"""Core pipeline hardening: loop resilience, infra circuit breaker, idempotent phases."""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

import pytest

from config.trading_constants import BANKROLL_USD
from obsidian_utils import ObsidianManager
from orchestrator import config as orch_config
from orchestrator import loop, phases, scraper
from orchestrator.openclaw_cli import OpenClawCLIError
from orchestrator.parse import run_agent_with_format_retries
from orchestrator.runner import spawn_agent
from orchestrator.schema_validation import AgentSchemaError
from orchestrator.scraper import MarketRow
from orchestrator.state import (
    InfraUnavailable,
    flag_inactive,
    is_inactive,
    market_quarantine,
)
from tests.orchestrator.test_phase3_helpers import (
    STUB_MARKET_DATA,
    briefer_ok,
    deep_researcher_complete,
    deep_researcher_needs_more,
)
from tests.orchestrator.test_phase5_archive import _seed_resolved_market

GOOD_DATA = {
    "yes_price": 0.3,
    "volume": 10_000.0,
    "liquidity": 20_000.0,
    "days_to_resolution": 10,
}
RESEARCH_MD = "## Bull Thesis\n\nb\n\n## Bear Thesis\n\nb\n\n## Post-Mortem\n"


@pytest.fixture()
def vault(tmp_path):
    v = ObsidianManager(vault_base=tmp_path)
    v.cold_start_protocol()
    return v


class _Stop(BaseException):
    pass


# --- 1. loop ---------------------------------------------------------------


def test_run_forever_survives_tick_failure_and_defers_phase6(vault, monkeypatch):
    ticks: list[int] = []
    phase6: list[int] = []

    def bad_tick(_v):
        ticks.append(1)
        raise RuntimeError("boom")

    def sleep(_s):
        if len(ticks) >= 3:
            raise _Stop

    monkeypatch.setattr(loop, "run_pipeline_tick", bad_tick)
    monkeypatch.setattr(loop, "phase6_macro_learning_loop", lambda v: phase6.append(1))
    monkeypatch.setattr(loop.time, "sleep", sleep)
    with pytest.raises(_Stop):
        loop.run_forever(vault)
    assert len(ticks) == 3
    assert phase6 == []  # not on the first tick(s): first run is after 24h


def test_run_forever_survives_phase6_failure(vault, monkeypatch):
    calls = {"tick": 0, "p6": 0}

    def tick(_v):
        calls["tick"] += 1
        if calls["tick"] == 1:
            raise InfraUnavailable("down")

    def p6(_v):
        calls["p6"] += 1
        raise RuntimeError("overseer exploded")

    def sleep(_s):
        if calls["tick"] >= 2:
            raise _Stop

    monkeypatch.setattr(loop, "OVERSEER_INTERVAL_SEC", 0)
    monkeypatch.setattr(loop, "run_pipeline_tick", tick)
    monkeypatch.setattr(loop, "phase6_macro_learning_loop", p6)
    monkeypatch.setattr(loop.time, "sleep", sleep)
    with pytest.raises(_Stop):
        loop.run_forever(vault)
    assert calls["tick"] == 2 and calls["p6"] >= 1


# --- 2. infra vs content errors -------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [OpenClawCLIError("gw down"), subprocess.TimeoutExpired("x", 1), ConnectionResetError()],
)
def test_infra_error_does_not_flag_and_aborts(vault, exc):
    vault.write_filter_log(
        "m1",
        {"market_id": "m1", "passed": True, "confidence_multiplier": 1.0, "details": "ok"},
    )
    with pytest.raises(InfraUnavailable):
        with market_quarantine(vault, "m1", "phase2"):
            raise exc
    assert not vault.is_market_inactive("m1")


def test_content_error_still_flags(vault):
    vault.write_filter_log(
        "m1",
        {"market_id": "m1", "passed": True, "confidence_multiplier": 1.0, "details": "ok"},
    )
    with market_quarantine(vault, "m1", "phase2"):
        raise KeyError("bad")
    assert vault.is_market_inactive("m1")


def test_phase2_gateway_outage_aborts_without_blacklisting(vault):
    def runner(role, payload):
        raise OpenClawCLIError("gateway down")

    market = MarketRow(market_id="m-out", market_title="T", market_data={})
    with pytest.raises(InfraUnavailable):
        phases.phase2_quantitative_routing(vault, [market], runner=runner)
    assert vault.read_filter_log("m-out") is None


def test_phase3_missing_price_skips_without_flagging(vault, monkeypatch):
    vault.write_filter_log(
        "m-np",
        {"market_id": "m-np", "passed": True, "confidence_multiplier": 1.0, "details": "ok"},
    )
    monkeypatch.setattr(
        scraper,
        "fetch_market_row",
        lambda mid: MarketRow(market_id=mid, market_title="T", market_data={"volume": 1.0}),
    )
    out = phases.phase3_qualitative_pipeline(vault, runner=lambda r, p: pytest.fail(r))
    assert out == []
    assert not vault.is_market_inactive("m-np")


# --- 3. flag_inactive minimal filter record --------------------------------


def test_flag_inactive_phase2_without_file_writes_minimal_record(vault):
    flag_inactive(vault, "m-new", "phase2", "evaluator error: x", {"junk": 1})
    rec = vault.read_filter_log("m-new")
    assert rec is not None and is_inactive(rec)
    assert rec["passed"] is False
    assert rec["confidence_multiplier"] == 0
    assert rec["details"] == "evaluator error: x"


def test_flag_inactive_corrupt_file_logs_loudly(vault, caplog):
    path = vault.active_research_path("m-bad")
    path.write_text("not frontmatter at all", encoding="utf-8")
    with caplog.at_level("ERROR"):
        flag_inactive(vault, "m-bad", "phase3", "why", {})
    assert any("corrupt" in r.message for r in caplog.records)


# --- 5. F1 post-mortem idempotency ----------------------------------------


def _resolved(monkeypatch):
    monkeypatch.setattr(
        scraper, "fetch_resolution", lambda mid: {"market_id": mid, "status": "closed"}
    )


def test_post_mortem_retries_then_flags_trade_not_post_mortem(vault, monkeypatch):
    _seed_resolved_market(vault, "0xpm")
    _resolved(monkeypatch)
    seen_orig: list[str] = []

    def failing(role, payload):
        seen_orig.append(payload["original_research"])
        return {"market_id": payload["market_id"], "post_mortem_analysis": "x", "error": "llm"}

    for attempt in (1, 2):
        phases.phase5_resolution_and_post_mortem(vault, runner=failing)
        trade = vault.read_trade_log_dict("0xpm")
        assert not is_inactive(trade)
        assert trade["post_mortem_attempts"] == attempt
    assert vault.post_mortem_path("0xpm").exists()

    phases.phase5_resolution_and_post_mortem(vault, runner=failing)  # 3rd: cap
    assert is_inactive(vault.read_trade_log_dict("0xpm"))
    assert not is_inactive(vault.read_market_record("0xpm", "post_mortem"))
    assert len(seen_orig) == 3


def test_post_mortem_recovers_on_retry_and_archives(vault, monkeypatch):
    _seed_resolved_market(vault, "0xok")
    _resolved(monkeypatch)
    state = {"n": 0}

    def flaky(role, payload):
        state["n"] += 1
        if state["n"] == 1:
            return {"market_id": payload["market_id"], "post_mortem_analysis": "x", "error": "llm"}
        return {"market_id": payload["market_id"], "post_mortem_analysis": "done", "error": None}

    phases.phase5_resolution_and_post_mortem(vault, runner=flaky)
    phases.phase5_resolution_and_post_mortem(vault, runner=flaky)
    assert vault.iter_open_trades() == []
    assert "done" in vault.read_post_mortem("0xok")


# --- 6/7/8. research bundle behaviour --------------------------------------


def _row(market_id: str) -> dict[str, Any]:
    return {
        "market_id": market_id,
        "market_title": "T",
        "market_description": "",
        "market_data": dict(STUB_MARKET_DATA),
    }


def test_poisoned_bundle_is_replanned_and_refetched(vault, monkeypatch):
    vault.write_research_bundle("m-p", [{"query": "q0", "research_data": "", "error": "timeout"}])
    fetched: list[list[str]] = []

    def fetch(qs):
        fetched.append(list(qs))
        return [{"query": q, "research_data": "real", "error": None} for q in qs]

    monkeypatch.setattr(phases, "fetch_research_bundle", fetch)
    roles: list[str] = []

    def runner(role, payload):
        roles.append(role)
        return briefer_ok(payload) if role == "briefer" else deep_researcher_complete(payload)

    out = phases._run_iterative_research(
        vault, runner, _row("m-p"), "d", from_edge=False, planning_context=None
    )
    assert out is not None and roles[0] == "briefer" and fetched
    assert any(e["research_data"] == "real" for e in vault.read_research_bundle("m-p"))


def test_usable_bundle_is_reused_without_planning(vault, monkeypatch):
    vault.write_research_bundle("m-u", [{"query": "q0", "research_data": "ok", "error": None}])

    def runner(role, payload):
        assert role == "deep_researcher"
        assert payload["q"] == STUB_MARKET_DATA["yes_price"]
        return deep_researcher_complete(payload)

    assert phases._run_iterative_research(
        vault, runner, _row("m-u"), "d", from_edge=False, planning_context=None
    )


def test_edge_refresh_always_plans_and_replaces_stale_rows(vault, monkeypatch):
    vault.write_research_bundle("m-e", [{"query": "stub query for T", "research_data": "STALE", "error": None}])
    monkeypatch.setattr(
        phases,
        "fetch_research_bundle",
        lambda qs: [{"query": q, "research_data": "FRESH", "error": None} for q in qs],
    )
    roles: list[str] = []

    def runner(role, payload):
        roles.append(role)
        if role == "briefer":
            assert payload["planning_context"] == "ctx"
            return briefer_ok(payload)
        return deep_researcher_complete(payload)

    phases._run_iterative_research(
        vault, runner, _row("m-e"), "d", from_edge=True, planning_context="ctx"
    )
    assert roles[0] == "briefer"
    assert [e["research_data"] for e in vault.read_research_bundle("m-e")] == ["FRESH"]


def test_forced_synthesis_fetches_last_pending_queries(vault, monkeypatch):
    fetched: list[str] = []

    def fetch(qs):
        fetched.extend(qs)
        return [{"query": q, "research_data": "d", "error": None} for q in qs]

    monkeypatch.setattr(phases, "fetch_research_bundle", fetch)
    n = {"i": 0}
    forced_bundle: list[Any] = []

    def runner(role, payload):
        if role == "briefer":
            return briefer_ok(payload)
        if payload.get("system_override"):
            forced_bundle.extend(e["query"] for e in payload["research_bundle"])
            return deep_researcher_complete(payload)
        n["i"] += 1
        return {"status": "needs_more_data", "new_queries": [f"follow-up {n['i']}"]}

    phases._run_iterative_research(
        vault, runner, _row("m-f"), "d", from_edge=False, planning_context=None
    )
    last = f"follow-up {n['i']}"
    assert last in fetched and last in forced_bundle


# --- 9. edge refresh attempt cap -------------------------------------------


def test_edge_refresh_attempts_capped_without_retry(vault, monkeypatch):
    mid = "m-cap2"
    vault.write_filter_log(
        mid, {"market_id": mid, "passed": True, "confidence_multiplier": 1.0, "details": "x"}
    )
    vault.write_research_report(mid, {"market_id": mid, "estimated_p": 0.5}, RESEARCH_MD)
    vault.write_trade_log(
        mid,
        {"market_id": mid, "allocation_usd": 0.0, "below_edge_threshold": True, "executed": False},
    )
    monkeypatch.setattr(scraper, "get_market_trends", lambda m, limit: [])
    monkeypatch.setattr(scraper, "trends_limit_for_filters", lambda: 10)
    calls: list[str] = []

    def runner(role, payload):
        calls.append(role)
        return {
            "market_id": mid, "passed": True, "trigger": None, "confidence_multiplier": 1.0,
            "details": "same", "error": None, "retry_deep_research": False, "refresh_reason": None,
        }

    market = MarketRow(market_id=mid, market_title="T", market_data={})
    for _ in range(6):
        phases.phase2_quantitative_routing(vault, [market], runner=runner)
    assert len(calls) == orch_config.max_edge_research_refreshes() == 3


# --- 10. Phase 4 ------------------------------------------------------------


@pytest.fixture()
def paper(monkeypatch):
    monkeypatch.setattr(phases, "PAPER_TRADE_MODE", True)


def _lying_executioner(calls: list[dict[str, Any]]):
    def runner(role, payload):
        assert role == "executioner"
        calls.append(payload)
        return {
            "market_id": payload["market_id"],
            "allocation_usd": 999999.0,
            "score": 42.0,
            "below_edge_threshold": True,
            "executed": True,
            "transaction_hash": "0xdead",
            "error": None,
        }

    return runner


def test_phase4_overrides_llm_allocation_and_forces_paper(vault, paper):
    calls: list[dict[str, Any]] = []
    row = {"market_id": "m4", "p_value": 0.8, "market_data": dict(GOOD_DATA)}
    phases.phase4_execution(vault, [row], runner=_lying_executioner(calls))
    payload = calls[0]
    assert payload["q"] == 0.3 and payload["allocation"]["error"] is None
    trade = vault.read_trade_log_dict("m4")
    expected = payload["allocation"]
    assert trade["allocation_usd"] == expected["allocation_usd"] > 0
    assert trade["allocation_usd"] < 999999
    assert trade["score"] == expected["score"]
    assert trade["below_edge_threshold"] is False
    assert trade["executed"] is False and trade["transaction_hash"] is None
    assert not vault.has_execution_intent("m4")


def test_phase4_remaining_bankroll_counts_open_trades(vault, paper):
    vault.write_trade_log(
        "other", {"market_id": "other", "allocation_usd": BANKROLL_USD - 1.0, "executed": False}
    )
    calls: list[dict[str, Any]] = []
    row = {"market_id": "m4b", "p_value": 0.8, "market_data": dict(GOOD_DATA)}
    phases.phase4_execution(vault, [row], runner=_lying_executioner(calls))
    assert vault.read_trade_log_dict("m4b")["allocation_usd"] <= 1.0 + 1e-9


def test_phase4_intent_and_trade_block_second_execution(vault, paper):
    calls: list[dict[str, Any]] = []
    row = {"market_id": "m4c", "p_value": 0.8, "market_data": dict(GOOD_DATA)}
    phases.phase4_execution(vault, [row], runner=_lying_executioner(calls))
    phases.phase4_execution(vault, [row], runner=_lying_executioner(calls))
    assert len(calls) == 1  # trade log blocks

    vault.write_execution_intent("m4d", {"market_id": "m4d"})
    row_d = {"market_id": "m4d", "p_value": 0.8, "market_data": dict(GOOD_DATA)}
    phases.phase4_execution(vault, [row_d], runner=_lying_executioner(calls))
    assert len(calls) == 1  # intent blocks


def test_phase4_failure_leaves_intent_and_not_reeligible(vault, paper):
    row = {"market_id": "m4e", "p_value": 0.8, "market_data": dict(GOOD_DATA)}
    vault.write_research_report("m4e", {"market_id": "m4e", "estimated_p": 0.8}, RESEARCH_MD)

    def failing(role, payload):
        return {"market_id": payload["market_id"], "allocation_usd": 0.0, "executed": False,
                "error": "venue rejected"}

    phases.phase4_execution(vault, [row], runner=failing)
    assert vault.has_execution_intent("m4e")
    assert vault.read_trade_log_dict("m4e") is None
    calls: list[dict[str, Any]] = []
    assert phases._phase4_queue(vault, []) == []  # vault scan will not re-queue it
    phases.phase4_execution(vault, [row], runner=_lying_executioner(calls))
    assert calls == []


def test_phase4_infra_failure_keeps_intent_and_aborts(vault, paper):
    row = {"market_id": "m4f", "p_value": 0.8, "market_data": dict(GOOD_DATA)}

    def down(role, payload):
        raise OpenClawCLIError("gateway down")

    with pytest.raises(InfraUnavailable):
        phases.phase4_execution(vault, [row], runner=down)
    assert vault.has_execution_intent("m4f")


def test_phase4_skips_when_q_unavailable(vault, paper):
    data = {**GOOD_DATA, "yes_price": None}
    data.pop("yes_price")
    row = {"market_id": "m4g", "p_value": 0.8, "market_data": data}
    phases.phase4_execution(vault, [row], runner=lambda r, p: pytest.fail("spawned"))
    assert not vault.has_execution_intent("m4g")


def test_phase4_vault_scan_queue(vault, paper, monkeypatch):
    for mid in ("scan-ok", "scan-traded", "scan-bad"):
        vault.write_research_report(mid, {"market_id": mid, "estimated_p": 0.8}, RESEARCH_MD)
    vault.write_trade_log("scan-traded", {"market_id": "scan-traded", "allocation_usd": 0.0, "executed": False})
    vault.write_research_report(
        "scan-bad", {"market_id": "scan-bad", "estimated_p": 0.8, "error": "broken"}, RESEARCH_MD
    )
    monkeypatch.setattr(
        scraper,
        "fetch_market_row",
        lambda mid: MarketRow(market_id=mid, market_title="T", market_data=dict(GOOD_DATA)),
    )
    calls: list[dict[str, Any]] = []
    # one researched this tick + a duplicate of a scanned market
    researched = [{"market_id": "scan-ok", "p_value": 0.8, "market_data": dict(GOOD_DATA)}]
    phases.phase4_execution(vault, researched, runner=_lying_executioner(calls))
    assert [c["market_id"] for c in calls] == ["scan-ok"]  # deduped, others filtered
    assert vault.read_trade_log_dict("scan-ok") is not None

    vault.write_research_report("scan-new", {"market_id": "scan-new", "estimated_p": 0.8}, RESEARCH_MD)
    calls.clear()
    phases.phase4_execution(vault, [], runner=_lying_executioner(calls))
    assert [c["market_id"] for c in calls] == ["scan-new"]


# --- 11. Phase 6 caps -------------------------------------------------------


def test_phase6_sends_bounded_recent_post_mortems(vault, caplog):
    import os

    for i in range(40):
        p = vault.post_mortem_path(f"pm{i:02d}")
        p.write_text("x" * 20_000, encoding="utf-8")
        os.utime(p, (1_000_000 + i, 1_000_000 + i))
    captured: dict[str, Any] = {}

    def runner(role, payload):
        captured.update(payload)
        return {"new_directives_markdown": payload["current_directives"], "rationale": "r", "error": None}

    with caplog.at_level("INFO"):
        phases.phase6_macro_learning_loop(vault, runner=runner)
    batch = captured["post_mortems"]
    assert len(batch) <= 30
    assert batch[0]["market_id"] == "pm39"  # most recent first
    assert all(len(b["content"]) <= 5 * 1024 for b in batch)
    assert sum(len(b["content"]) for b in batch) <= 90 * 1024
    assert any("dropped" in r.message for r in caplog.records)


# --- 12/13. format retries, runner mode, stubs ------------------------------


def test_format_retry_on_agent_schema_error():
    calls: list[dict[str, Any]] = []

    def runner(role, payload):
        calls.append(payload)
        if len(calls) == 1:
            raise AgentSchemaError(role, "output", "bad shape")
        return {"ok": True}

    out = run_agent_with_format_retries(runner, "briefer", {"a": 1}, validate_fn=lambda d: d)
    assert out == {"ok": True}
    assert "bad shape" in calls[1]["format_validation_error"]


def test_format_retry_gives_up_and_input_errors_are_not_retried():
    def always(role, payload):
        raise AgentSchemaError(role, "output", "bad")

    with pytest.raises(AgentSchemaError):
        run_agent_with_format_retries(always, "briefer", {}, validate_fn=lambda d: d)

    n = {"c": 0}

    def input_err(role, payload):
        n["c"] += 1
        raise AgentSchemaError(role, "input", "missing q")

    with pytest.raises(AgentSchemaError):
        run_agent_with_format_retries(input_err, "briefer", {}, validate_fn=lambda d: d)
    assert n["c"] == 1


def test_runner_mode_rejects_unknown(monkeypatch):
    monkeypatch.setenv(orch_config.RUNNER_MODE_ENV, "lvie")
    with pytest.raises(ValueError):
        orch_config.runner_mode()
    monkeypatch.setenv(orch_config.RUNNER_MODE_ENV, " LIVE ")
    assert orch_config.runner_mode() == "live"


def test_stub_responses_satisfy_new_schemas(monkeypatch):
    monkeypatch.setenv(orch_config.RUNNER_MODE_ENV, "stub")
    alloc = {"allocation_usd": 0.0, "score": 0.0, "below_edge_threshold": True, "error": None}
    spawn_agent(
        "executioner",
        {"market_id": "m", "p_value": 0.5, "q": 0.4, "allocation": alloc,
         "market_data": {}, "paper_trade_mode": True},
    )
    spawn_agent(
        "deep_researcher",
        {"market_id": "m", "market_data": {}, "q": 0.4, "directives": "",
         "research_bundle": [], "system_override": None, "format_validation_error": None},
    )
    spawn_agent(
        "re_evaluator",
        {"market_id": "m", "review_kind": "quantitative", "filter_directives": {},
         "historic_signal_bundle": None, "prior_filter_trigger": None,
         "prior_evaluator_details": None, "prior_filter_log": None,
         "prior_filter_result": None, "research_markdown": None, "trade_log": None},
    )
    from obsidian_utils import DirectivesPayload

    out = spawn_agent("overseer", {"post_mortems": [], "current_directives": ""})
    DirectivesPayload.model_validate(out)
