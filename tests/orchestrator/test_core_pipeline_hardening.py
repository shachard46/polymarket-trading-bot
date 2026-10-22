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
from orchestrator import ledger, loop, phases, scraper
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
GATE_OK = {
    "yes_price": 0.4,
    "no_price": 0.6,
    "volume": 1_000.0,
    "liquidity": 20_000.0,
    "days_to_resolution": 30,
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

    market = MarketRow(market_id="m-out", market_title="T", market_data=dict(GATE_OK))
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
    flag_inactive(vault, "m-new", "phase2", "triage error: x", {"junk": 1})
    rec = vault.read_filter_log("m-new")
    assert rec is not None and is_inactive(rec)
    assert rec["passed"] is False
    assert rec["confidence_multiplier"] == 0
    assert rec["details"] == "triage error: x"


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

    out = phases._research_market(vault, runner, _row("m-p"), "d")
    assert out is not None and roles[0] == "briefer" and fetched
    assert any(e["research_data"] == "real" for e in vault.read_research_bundle("m-p"))


def test_usable_bundle_is_reused_without_planning(vault, monkeypatch):
    vault.write_research_bundle("m-u", [{"query": "q0", "research_data": "ok", "error": None}])

    def runner(role, payload):
        assert role == "deep_researcher"
        assert "q" not in payload and "yes_price" not in payload
        return deep_researcher_complete(payload)

    assert phases._research_market(vault, runner, _row("m-u"), "d")


def test_refresh_always_plans_and_replaces_stale_rows(vault, monkeypatch):
    vault.write_research_bundle("m-e", [{"query": "stale q", "research_data": "STALE", "error": None}])
    monkeypatch.setattr(
        phases,
        "fetch_research_bundle",
        lambda qs: [{"query": q, "research_data": "FRESH", "error": None} for q in qs],
    )
    roles: list[str] = []

    def runner(role, payload):
        roles.append(role)
        return briefer_ok(payload) if role == "briefer" else deep_researcher_complete(payload)

    phases._research_market(vault, runner, _row("m-e"), "d", refresh=True)
    assert roles[0] == "briefer"
    data = [e["research_data"] for e in vault.read_research_bundle("m-e")]
    assert data and set(data) == {"FRESH"}


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

    phases._research_market(vault, runner, _row("m-f"), "d")
    last = f"follow-up {n['i']}"
    assert n["i"] == 2  # one round + one follow-up round, then forced synthesis
    assert last in fetched and last in forced_bundle


# --- 10. Phase 4 (deterministic) -------------------------------------------


@pytest.fixture()
def trades(monkeypatch):
    """Spy around the real (paper) ``execute_polymarket_trade`` skill."""
    calls: list[dict[str, Any]] = []
    real = phases._load_execute_polymarket_trade()

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(phases, "_load_execute_polymarket_trade", lambda: spy)
    return calls


def _fake_trade(monkeypatch, calls, **result):
    out = {"success": True, "transaction_hash": "0xabc", "paper": False, "error": None, **result}

    def fn(**kwargs):
        calls.append(kwargs)
        return out

    monkeypatch.setattr(phases, "_load_execute_polymarket_trade", lambda: fn)


def _p4row(market_id, p, data=None, **extra):
    return {"market_id": market_id, "p_value": p, "market_data": dict(data or GOOD_DATA), **extra}


def test_phase4_yes_trade_calls_skill_and_logs(vault, trades):
    row = _p4row("m4", 0.8, p_low=0.7, p_high=0.9)
    phases.phase4_execution(vault, [row])
    assert len(trades) == 1
    call = trades[0]
    trade = vault.read_trade_log_dict("m4")
    assert call["market_id"] == "m4" and call["outcome"] == "YES"
    assert call["amount"] == trade["allocation_usd"] > 0
    assert call["price"] == trade["price"]
    assert (trade["side"], trade["q_entry"], trade["p"]) == ("YES", 0.3, 0.8)
    assert (trade["p_low"], trade["p_high"]) == (0.7, 0.9)
    assert trade["shares"] > 0 and trade["below_edge_threshold"] is False
    assert trade["executed"] is False  # paper
    assert str(trade["transaction_hash"]).startswith("PAPER-")
    assert not vault.has_execution_intent("m4")
    rows = [r for r in ledger.iter_rows(vault) if r["market_id"] == "m4"]
    assert [r["stage"] for r in rows] == ["traded"]
    assert rows[0]["side"] == "YES" and rows[0]["p_low"] == 0.7


def test_phase4_no_trade_buys_the_no_side(vault, trades):
    data = {**GOOD_DATA, "yes_price": 0.7}
    phases.phase4_execution(vault, [_p4row("m4no", 0.1, data)])
    assert trades[0]["outcome"] == "NO"
    trade = vault.read_trade_log_dict("m4no")
    assert trade["side"] == "NO" and trade["allocation_usd"] > 0
    assert trade["price"] == pytest.approx(0.3)  # NO ask = 1 - q


def test_phase4_live_result_marks_executed(vault, monkeypatch):
    calls: list[dict[str, Any]] = []
    _fake_trade(monkeypatch, calls, paper=False)
    phases.phase4_execution(vault, [_p4row("m4live", 0.8)])
    trade = vault.read_trade_log_dict("m4live")
    assert trade["executed"] is True and trade["transaction_hash"] == "0xabc"


def test_phase4_below_edge_skips_the_skill_and_ledgers(vault, trades):
    phases.phase4_execution(vault, [_p4row("m4be", 0.31)])
    assert trades == []
    trade = vault.read_trade_log_dict("m4be")
    assert trade["allocation_usd"] == 0 and trade["below_edge_threshold"] is True
    assert trade["executed"] is False and trade["transaction_hash"] is None
    assert not vault.has_execution_intent("m4be")
    stages = [r["stage"] for r in ledger.iter_rows(vault) if r["market_id"] == "m4be"]
    assert stages == ["below_edge"]


def test_phase4_remaining_bankroll_counts_open_trades(vault, trades):
    vault.write_trade_log(
        "other", {"market_id": "other", "allocation_usd": BANKROLL_USD - 1.0, "executed": False}
    )
    phases.phase4_execution(vault, [_p4row("m4b", 0.8)])
    assert vault.read_trade_log_dict("m4b")["allocation_usd"] <= 1.0 + 1e-9


def test_phase4_intent_and_trade_block_second_execution(vault, trades):
    row = _p4row("m4c", 0.8)
    phases.phase4_execution(vault, [row])
    phases.phase4_execution(vault, [row])
    assert len(trades) == 1  # trade log blocks

    vault.write_execution_intent("m4d", {"market_id": "m4d"})
    phases.phase4_execution(vault, [_p4row("m4d", 0.8)])
    assert len(trades) == 1  # intent blocks


def test_phase4_failure_leaves_intent_and_not_reeligible(vault, monkeypatch):
    row = _p4row("m4e", 0.8)
    vault.write_research_report("m4e", {"market_id": "m4e", "estimated_p": 0.8}, RESEARCH_MD)
    calls: list[dict[str, Any]] = []
    _fake_trade(monkeypatch, calls, success=False, transaction_hash=None, error="venue rejected")

    phases.phase4_execution(vault, [row])
    assert vault.has_execution_intent("m4e")
    assert vault.read_trade_log_dict("m4e") is None
    assert vault.is_market_inactive("m4e", dir_key="active")
    assert phases._phase4_queue(vault, []) == []  # vault scan will not re-queue it
    phases.phase4_execution(vault, [row])
    assert len(calls) == 1


def test_phase4_infra_failure_keeps_intent_and_aborts(vault, monkeypatch):
    def down(**_kw):
        raise OpenClawCLIError("gateway down")

    monkeypatch.setattr(phases, "_load_execute_polymarket_trade", lambda: down)
    with pytest.raises(InfraUnavailable):
        phases.phase4_execution(vault, [_p4row("m4f", 0.8)])
    assert vault.has_execution_intent("m4f")


def test_phase4_skips_when_q_unavailable(vault, trades):
    data = dict(GOOD_DATA)
    data.pop("yes_price")
    phases.phase4_execution(vault, [_p4row("m4g", 0.8, data)])
    assert trades == [] and not vault.has_execution_intent("m4g")


def test_phase4_vault_scan_queue(vault, trades, monkeypatch):
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
    # one researched this tick + a duplicate of a scanned market
    researched = [_p4row("scan-ok", 0.8)]
    phases.phase4_execution(vault, researched)
    assert [c["market_id"] for c in trades] == ["scan-ok"]  # deduped, others filtered
    assert vault.read_trade_log_dict("scan-ok") is not None

    vault.write_research_report(
        "scan-new",
        {"market_id": "scan-new", "estimated_p": 0.8, "p_low": 0.7, "p_high": 0.9},
        RESEARCH_MD,
    )
    trades.clear()
    phases.phase4_execution(vault, [])
    assert [c["market_id"] for c in trades] == ["scan-new"]
    assert vault.read_trade_log_dict("scan-new")["p_high"] == 0.9


def test_phase4_edge_dq_trade_reexecutes_after_fresh_research(vault, trades):
    phases.phase4_execution(vault, [_p4row("m4r", 0.31)])  # below edge -> trade log, no skill call
    assert trades == []
    phases.phase4_execution(vault, [_p4row("m4r", 0.8)])  # fresh research: re-executes
    assert len(trades) == 1 and vault.read_trade_log_dict("m4r")["side"] == "YES"


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
    spawn_agent(
        "deep_researcher",
        {"market_id": "m", "title": "T", "description": "", "resolution_source": None,
         "end_date": None, "category": None, "today": "2026-01-01", "mode": "quick",
         "directives": "", "research_bundle": [], "system_override": None,
         "format_validation_error": None, "session_tag": None},
    )
    spawn_agent(
        "triage",
        {"market_id": "m", "title": "T", "description": "", "resolution_source": None,
         "category": None, "end_date": None, "today": "2026-01-01"},
    )
    from obsidian_utils import DirectivesPayload

    out = spawn_agent("overseer", {"post_mortems": [], "current_directives": ""})
    DirectivesPayload.model_validate(out)
