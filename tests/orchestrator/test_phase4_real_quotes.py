"""Phase 4 sizes on the real CLOB asks and caps the order by ask depth; Phase 5 resolves via the API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from obsidian_utils import ObsidianManager
from orchestrator import ledger, phases, polymarket_api
from orchestrator.polymarket_api import PolymarketAPIError

DATA = {"yes_price": 0.3, "volume": 10_000.0, "liquidity": 20_000.0, "days_to_resolution": 10}


@pytest.fixture()
def vault(tmp_path):
    v = ObsidianManager(vault_base=tmp_path)
    v.cold_start_protocol()
    return v


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
