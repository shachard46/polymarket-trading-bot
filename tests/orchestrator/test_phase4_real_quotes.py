"""Phase 4 sizes on the real CLOB asks and caps the order by ask depth; Phase 5 resolves via the API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from config.trading_constants import BANKROLL_USD
from orchestrator import ledger, phases, polymarket_api, scraper
from orchestrator.llm.base import LLMError
from orchestrator.phases import p4_execution
from orchestrator.polymarket_api import PolymarketAPIError
from orchestrator.scraper import MarketRow
from orchestrator.state import InfraUnavailable
from tests.orchestrator.conftest import QUOTE_HINTS

DATA = {"yes_price": 0.3, "volume": 10_000.0, "liquidity": 20_000.0, "days_to_resolution": 10}


RESEARCH_MD = "## Bull Thesis\n\nb\n\n## Bear Thesis\n\nb\n\n## Post-Mortem\n"



def quotes(monkeypatch, yes=0.32, no=0.72, yes_depth=1e9, no_depth=1e9):
    def fake(market_id: str, side: str):
        ask, depth = (yes, yes_depth) if side == "YES" else (no, no_depth)
        return {"ask": ask, "depth_usd_within_2c": depth, "spread": 0.02, "token_id": side}

    monkeypatch.setattr(polymarket_api, "executable_quote", fake)


def run(vault, market_id, p, data=None, **extra):
    row = {"market_id": market_id, "p_value": p, "market_data": dict(data or DATA), **extra}
    phases.phase4_execution(vault, [row])


def test_phase4_prices_the_trade_at_the_real_yes_ask_not_the_mid(vault, monkeypatch):
    quotes(monkeypatch, yes=0.32, no=0.72)
    run(vault, "q1", 0.8, p_low=0.7, p_high=0.9)
    trade = vault.read_trade_log_dict("q1")
    assert trade["side"] == "YES" and trade["price"] == 0.32 and trade["ask"] == 0.32
    assert trade["q_entry"] == 0.3  # the mid stays the research reference
    assert trade["shares"] == pytest.approx(trade["allocation_usd"] / 0.32)
    row = next(r for r in ledger.iter_rows(vault) if r["market_id"] == "q1")
    assert (row["stage"], row["price"], row["ask"]) == ("traded", 0.32, 0.32)


def test_phase4_buys_no_at_the_real_no_ask(vault, monkeypatch):
    quotes(monkeypatch, yes=0.71, no=0.31)
    run(vault, "q2", 0.1, {**DATA, "yes_price": 0.7})
    trade = vault.read_trade_log_dict("q2")
    assert trade["side"] == "NO" and trade["price"] == 0.31 and trade["ask"] == 0.31


def test_phase4_caps_the_order_at_ask_depth_within_2c(vault, monkeypatch):
    quotes(monkeypatch, yes_depth=1e9)
    run(vault, "deep", 0.8)
    uncapped = vault.read_trade_log_dict("deep")["allocation_usd"]
    assert uncapped > 40

    quotes(monkeypatch, yes_depth=40.0)
    run(vault, "thin", 0.8)
    trade = vault.read_trade_log_dict("thin")
    assert trade["allocation_usd"] == pytest.approx(40.0)
    assert trade["depth_usd"] == 40.0 and trade["below_edge_threshold"] is False


def test_phase4_depth_below_min_order_means_no_trade(vault, monkeypatch):
    quotes(monkeypatch, yes_depth=2.0)
    run(vault, "dust", 0.8)
    trade = vault.read_trade_log_dict("dust")
    assert trade["allocation_usd"] == 0 and trade["executed"] is False


@pytest.mark.parametrize("failure", ["raise", "none"])
def test_phase4_quote_failure_skips_this_tick_without_flagging(vault, monkeypatch, failure):
    def fake(market_id, side):
        if failure == "raise":
            raise PolymarketAPIError("CLOB down")
        return None

    monkeypatch.setattr(polymarket_api, "executable_quote", fake)
    run(vault, "q3", 0.8)
    assert vault.read_trade_log_dict("q3") is None
    assert not vault.has_execution_intent("q3")
    assert not vault.is_market_inactive("q3", dir_key="active")
    assert [r for r in ledger.iter_rows(vault) if r["market_id"] == "q3"] == []
    quotes(monkeypatch)  # next tick succeeds
    run(vault, "q3", 0.8)
    assert vault.read_trade_log_dict("q3")["side"] == "YES"


def test_ledger_sweep_resolves_a_market_that_was_only_gate_rejected(vault):
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "polymarket" / "market_closed_resolved.json").read_text()
    )
    cid = fixture["conditionId"]
    ledger.record(vault, cid, "gate_reject", reason="price_extreme", q=0.02)

    class Sess:
        def get(self, url, params=None, timeout=None):
            payload: Any = [fixture] if params.get("closed") == "true" else []

            class R:
                status_code = 200
                headers: dict[str, str] = {}

                def json(self):
                    return payload

            return R()

    polymarket_api.set_session(Sess())
    try:
        phases.phase5_resolution_and_post_mortem(vault)
    finally:
        polymarket_api.set_session(None)
    res = [r for r in ledger.iter_rows(vault) if r["market_id"] == cid and r["stage"] == "resolution"]
    assert len(res) == 1 and res[0]["outcome_yes"] == 1
    assert res[0]["raw_outcome"] == ["1", "0"]


@pytest.fixture()
def trades(monkeypatch):
    """Spy around the real (paper) ``execute_polymarket_trade`` skill."""
    calls: list[dict[str, Any]] = []
    real = phases._load_execute_polymarket_trade()

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(p4_execution, "_load_execute_polymarket_trade", lambda: spy)
    return calls


def _fake_trade(monkeypatch, calls, **result):
    out = {"success": True, "transaction_hash": "0xabc", "paper": False, "error": None, **result}

    def fn(**kwargs):
        calls.append(kwargs)
        return out

    monkeypatch.setattr(p4_execution, "_load_execute_polymarket_trade", lambda: fn)


def _p4row(market_id, p, data=None, **extra):
    md = dict(data or DATA)
    QUOTE_HINTS[market_id] = md  # the default fake CLOB quote follows the row's prices
    return {"market_id": market_id, "p_value": p, "market_data": md, **extra}


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
        raise LLMError("provider down")

    monkeypatch.setattr(p4_execution, "_load_execute_polymarket_trade", lambda: down)
    with pytest.raises(InfraUnavailable):
        phases.phase4_execution(vault, [_p4row("m4f", 0.8)])
    assert vault.has_execution_intent("m4f")


def test_phase4_skips_when_q_unavailable(vault, trades):
    data = dict(DATA)
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
        lambda mid: MarketRow(market_id=mid, market_title="T", market_data=dict(DATA)),
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
