"""Deterministic Phase 2 gate: eligibility, score, backoff, arbitrage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from config.trading_constants import FILTERS, MAX_DAYS_TO_RESOLUTION, MIN_DAYS_TO_RESOLUTION
from orchestrator import gate

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def row(**over):
    base = {
        "yes_price": 0.4,
        "no_price": 0.6,
        "volume": 1_000.0,
        "liquidity": 20_000.0,
        "days_to_resolution": 30,
    }
    base.update(over)
    return base


def test_integrity_rejects_and_missing_passes():
    assert gate.eligibility(row(integ_ramp_30d=0.3, integ_vol_30d_usd=1000)) == (False, "ramp_thin")
    assert gate.eligibility(row(integ_ramp_30d=0.3, integ_vol_30d_usd=30_000)) == (True, "ok")
    assert gate.eligibility(row(integ_ramp_30d=0.2)) == (True, "ok")  # volume missing
    assert gate.eligibility(row(integ_top1_wallet_share=0.7, integ_n_trades_30d=12)) == (False, "wallet_dominated")
    assert gate.eligibility(row(integ_top1_wallet_share=0.7, integ_n_trades_30d=5)) == (True, "ok")
    assert gate.eligibility(row(integ_n_wallets_30d=3, integ_ramp_30d=0.16, integ_vol_30d_usd=99_999)) == (
        False,
        "wallet_dominated",
    )
    assert gate.eligibility(row(integ_n_wallets_30d=3)) == (True, "ok")
    assert gate.eligibility(row(integ_rules_changed=True)) == (False, "rules_changed")
    assert gate.eligibility(row(integ_rules_changed=False)) == (True, "ok")
    assert gate.eligibility(row(integ_n_disputes=1)) == (False, "disputed_before")
    assert gate.eligibility(row(integ_n_disputes=0)) == (True, "ok")
    assert gate.eligibility({"market_data": row(integ_n_disputes=2)}) == (False, "disputed_before")


def test_eligible_market_ok():
    assert gate.eligibility(row()) == (True, "ok")
    assert gate.eligibility({"market_data": row()}) == (True, "ok")


@pytest.mark.parametrize(
    "over,reason",
    [
        ({"yes_price": 0.97, "no_price": 0.03}, "price_extreme"),
        ({"yes_price": 0.04, "no_price": 0.96}, "price_extreme"),
        ({"yes_price": None, "no_price": None}, "no_price"),
        ({"days_to_resolution": 3}, "too_soon"),
        ({"days_to_resolution": 90}, "too_far"),
        ({"days_to_resolution": None}, "no_end_date"),
        ({"liquidity": 50.0}, "illiquid"),
        ({"liquidity": None}, "illiquid"),
        ({"status": "closed"}, "closed"),
    ],
)
def test_eligibility_reasons(over, reason):
    ok, why = gate.eligibility(row(**over))
    assert (ok, why) == (False, reason)


def test_eligibility_boundaries_inclusive():
    assert gate.eligibility(row(days_to_resolution=MIN_DAYS_TO_RESOLUTION))[0]
    assert gate.eligibility(row(days_to_resolution=MAX_DAYS_TO_RESOLUTION))[0]
    assert gate.eligibility(row(liquidity=100.0))[0]
    assert gate.eligibility(row(yes_price=FILTERS["gate_min_q"]))[0]
    assert gate.eligibility(row(yes_price=FILTERS["gate_max_q"]))[0]


def test_score_prefers_low_attention_sweet_spot_and_mid_price():
    base = gate.score(row())
    assert gate.score(row(volume=500_000.0)) < base  # crowded
    assert gate.score(row(days_to_resolution=8)) < base
    assert gate.score(row(days_to_resolution=58)) < base
    assert gate.score(row(days_to_resolution=14)) == gate.score(row(days_to_resolution=45))
    assert gate.score(row(yes_price=0.1)) < base
    assert gate.score(row(yes_price=0.5)) > gate.score(row(yes_price=0.3))


def test_arbitrage_uses_real_asks_when_present():
    assert gate.arbitrage_combined(row(yes_ask=0.4, no_ask=0.5)) == pytest.approx(0.9)
    assert gate.arbitrage_combined(row(yes_ask=0.5, no_ask=0.5)) is None
    # Asks win over the mid-based prices; prices are the fallback.
    assert gate.arbitrage_combined(row(yes_price=0.4, no_price=0.5, yes_ask=0.5, no_ask=0.55)) is None
    assert gate.arbitrage_combined(row(yes_price=0.4, no_price=0.5)) == pytest.approx(0.9)


def test_backoff_schedule_and_cap():
    hours = [gate.backoff_delay(n).total_seconds() / 3600 for n in range(1, 8)]
    assert hours == [gate.BACKOFF_BASE_HOURS * 2**i for i in range(6)] + [gate.BACKOFF_CAP_HOURS]
    assert gate.backoff_delay(0) == timedelta(0)


def test_failures_back_off_then_retire():
    rec: dict = {}
    for n in range(1, gate.MAX_FAILS):
        rec.update(gate.register_failure(rec, 0.5, NOW))
        assert rec["fail_count"] == n
        assert "status" not in rec
        assert gate.is_due(rec, NOW + gate.backoff_delay(n)) is True
        assert gate.is_due(rec, NOW + gate.backoff_delay(n) - timedelta(minutes=1)) is False
    rec.update(gate.register_failure(rec, 0.5, NOW))
    assert rec["fail_count"] == gate.MAX_FAILS and rec["status"] == "retired"
    assert gate.is_retired(rec)


def test_price_move_resets_fail_count():
    rec = {"fail_count": 4, "last_q": 0.50}
    assert gate.register_failure(rec, 0.52, NOW)["fail_count"] == 5
    out = gate.register_failure(rec, 0.60, NOW)
    assert out["fail_count"] == 1 and out["last_q"] == 0.60


def test_register_pass_clears_backoff():
    assert gate.register_pass(0.4) == {"fail_count": 0, "next_check_at": None, "last_q": 0.4}
    assert gate.is_due({"next_check_at": None}, NOW)
    assert gate.is_due(None, NOW)


def test_arbitrage_detection():
    assert gate.arbitrage_combined(row(yes_price=0.45, no_price=0.5)) == pytest.approx(0.95)
    assert gate.arbitrage_combined(row(yes_price=0.5, no_price=0.5)) is None
    assert gate.arbitrage_combined(row(yes_price=0.0, no_price=0.5)) is None
    assert gate.arbitrage_combined(row(yes_price=None)) is None


def test_suspicious_move_rejected_on_thin_volume():
    assert gate.eligibility(row(day_price_change=-0.2, volume_24h=1_000.0)) == (False, "suspicious_move")
    assert gate.eligibility(row(day_price_change=0.2, volume_24h=50_000.0)) == (True, "ok")
    assert gate.eligibility(row(day_price_change=0.05, volume_24h=1_000.0)) == (True, "ok")


def test_concentrated_rejected_only_on_supply_basis():
    assert gate.eligibility(row(holder_top1=0.6, holder_top3=0.7, holder_basis="supply")) == (False, "concentrated")
    assert gate.eligibility(row(holder_top1=0.2, holder_top3=0.8, holder_basis="supply")) == (False, "concentrated")
    assert gate.eligibility(row(holder_top1=0.2, holder_top3=0.4, holder_basis="supply")) == (True, "ok")
    # top_n basis overstates concentration: record-only, never rejects
    assert gate.eligibility(row(holder_top1=0.9, holder_top3=0.99, holder_basis="top_n")) == (True, "ok")
    assert gate.eligibility(row(holder_top1=0.9, holder_top3=0.99)) == (True, "ok")


def test_wide_spread_rejected():
    assert gate.eligibility(row(yes_bid=0.30, yes_ask=0.45)) == (False, "wide_spread")
    assert gate.eligibility(row(yes_bid=0.39, yes_ask=0.41)) == (True, "ok")


def test_missing_manipulation_signals_pass():
    assert gate.eligibility(row(yes_ask=0.9)) == (True, "ok")
    assert gate.eligibility(row(day_price_change=0.5)) == (True, "ok")
    assert gate.eligibility(row(volume_24h=0.0)) == (True, "ok")
