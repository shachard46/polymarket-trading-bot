"""Loop resilience, infra circuit breaker, idempotent phases, format retries."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from orchestrator import config as orch_config
from orchestrator import loop, phases, scraper
from orchestrator.openclaw_cli import OpenClawCLIError
from orchestrator.parse import run_agent_with_format_retries
from orchestrator.schema_validation import AgentSchemaError
from orchestrator.scraper import MarketRow
from orchestrator.state import (
    InfraUnavailable,
    flag_inactive,
    is_inactive,
    market_quarantine,
)
from tests.orchestrator.test_phase5_archive import _seed_resolved_market

GATE_OK = {
    "yes_price": 0.4,
    "no_price": 0.6,
    "volume": 1_000.0,
    "liquidity": 20_000.0,
    "days_to_resolution": 30,
}
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


# --- 11. Phase 6 caps -------------------------------------------------------


def test_phase6_sends_bounded_recent_post_mortems(vault, caplog, monkeypatch):
    import os

    monkeypatch.setenv("OVERSEER_MIN_NEW_RESOLVED", "0")

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
    assert len(batch) <= 15
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
