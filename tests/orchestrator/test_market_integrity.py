"""Synthetic cases for orchestrator.market_integrity: one per deception pattern."""

from __future__ import annotations

import json

import pytest

from orchestrator import market_integrity as mi

NOW = 1_800_000_000.0
DAY = 86400.0


def hist(prices):
    n = len(prices)
    return [(NOW - (n - 1 - i) * DAY, p) for i, p in enumerate(prices)]


def trade(wallet, side, size, price, days_ago, outcome_index=0):
    return {"proxy_wallet": wallet, "side": side, "size": size, "price": price,
            "timestamp": NOW - days_ago * DAY, "outcome_index": outcome_index}


def test_ramp_features():
    f = mi.features(hist([0.2] * 20 + [0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.5, 0.6]), None, None, None, None, NOW)
    assert f["integ_ramp_30d"] == pytest.approx(0.4)
    assert f["integ_ramp_7d"] == pytest.approx(0.4)
    assert f["integ_ramp_step_share"] == pytest.approx(1.0)  # 0.3 + 0.1 steps carry the move


def test_ramp_gradual_has_low_step_share():
    f = mi.features(hist([0.1 + 0.01 * i for i in range(30)]), None, None, None, None, NOW)
    assert f["integ_ramp_30d"] == pytest.approx(0.29)
    assert f["integ_ramp_step_share"] == pytest.approx(2 / 29)


def test_trade_concentration_and_wash():
    trades = [trade("a", "BUY", 100, 0.5, 1), trade("a", "SELL", 100, 0.5, 2),
              trade("b", "BUY", 50, 0.5, 3), trade("c", "BUY", 50, 0.5, 40)]  # c outside window
    f = mi.features(None, trades, None, None, None, NOW)
    assert f["integ_vol_30d_usd"] == pytest.approx(125.0)
    assert f["integ_n_trades_30d"] == 3
    assert f["integ_n_wallets_30d"] == 2
    assert f["integ_top1_wallet_share"] == pytest.approx(100 / 125)
    assert f["integ_top3_wallet_share"] == pytest.approx(1.0)
    assert f["integ_two_sided_wallet_share"] == pytest.approx(100 / 125)
    assert f["integ_days_since_trade"] == pytest.approx(1.0)


def test_ramp_and_volume_combo_and_spike():
    trades = [trade("a", "BUY", 1000, 0.5, 5)]  # 500 usd
    md = {"volume_24h": 100.0}
    f = mi.features(hist([0.1, 0.4]), trades, None, md, None, NOW)
    assert f["integ_move_per_kusd"] == pytest.approx(0.3 / 0.5)
    assert f["integ_vol_spike"] == pytest.approx(100 / (500 / 30))


def test_stale_last_vs_mid():
    f = mi.features(None, None, None, {"yes_bid": 0.4, "yes_ask": 0.5, "last_trade_price": 0.2}, None, NOW)
    assert f["integ_last_vs_mid"] == pytest.approx(0.25)


def test_book_depth_wall_and_liq():
    book = {"bids": [(0.49, 100), (0.40, 1000)], "asks": [(0.51, 1000), (0.52, 10), (0.60, 5000)]}
    f = mi.features(None, None, book, {"liquidity": 5000.0, "rewards_min_size": 50}, None, NOW)
    depth = 0.49 * 100 + 0.51 * 1000 + 0.52 * 10
    assert f["integ_depth_2c_usd"] == pytest.approx(depth)
    assert f["integ_liq_to_depth"] == pytest.approx(5000 / depth)
    assert f["integ_ask_wall_share"] == pytest.approx(510 / (510 + 5.2))
    assert f["integ_rewards_on"] is True


def test_rules_changed():
    md = {"description": "Resolves YES if X."}
    first = mi.features(None, None, None, md, None, NOW)
    h = first["integ_description_hash"]
    assert h and first["integ_rules_changed"] is False
    same = mi.features(None, None, None, md, {"description_first_hash": h}, NOW)
    assert same["integ_rules_changed"] is False
    diff = mi.features(None, None, None, {"description": "Resolves YES if Y."}, {"description_first_hash": h}, NOW)
    assert diff["integ_rules_changed"] is True


def test_resolution_games_and_age():
    md = {"uma_resolution_statuses": json.dumps(["proposed", "disputed", "proposed"]),
          "custom_liveness": 7200, "resolution_mechanism": "uma", "created_at": "2026-12-31T00:00:00Z"}
    f = mi.features(None, None, None, md, None, 1_798_761_600.0 + 3 * DAY)  # 2027-01-01 base arbitrary
    assert f["integ_n_disputes"] == 1
    assert f["integ_custom_liveness"] == 7200
    assert f["integ_resolution_mechanism"] == "uma"
    assert f["integ_age_days"] is not None and f["integ_age_days"] >= 0
    assert mi.features(None, None, None, {"uma_resolution_statuses": ["disputed", "disputed"]}, None, NOW)["integ_n_disputes"] == 2


def test_all_none_inputs():
    f = mi.features(None, None, None, None, None, NOW)
    assert set(f) >= {"integ_ramp_7d", "integ_age_days", "integ_n_disputes", "integ_depth_2c_usd"}
    assert all(v is None or v is False for v in f.values())
    assert mi.score(f) is None
    assert mi.flags(f) == []


def test_score_bounds_and_flags():
    bad = {"integ_ramp_30d": 5.0, "integ_top1_wallet_share": 1.0, "integ_two_sided_wallet_share": 1.0,
           "integ_days_since_trade": 100, "integ_vol_spike": 100, "integ_liq_to_depth": 1e6,
           "integ_ask_wall_share": 1.0, "integ_age_days": 0}
    assert mi.score(bad) == 0.0
    assert len(mi.flags(bad)) == 8
    clean = {"integ_ramp_30d": 0.0, "integ_top1_wallet_share": 0.0, "integ_age_days": 100}
    assert mi.score(clean) == 1.0
    assert mi.flags(clean) == []
    mixed = {"integ_ramp_30d": 0.15, "integ_top1_wallet_share": 0.0}
    assert mi.score(mixed) == pytest.approx(0.75)
    assert mi.flags(mixed) == ["ramp"]
