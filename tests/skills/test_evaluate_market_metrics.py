"""Unit tests for evaluate_market_metrics skill (no live DB)."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from config.trading_constants import FILTERS

_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "evaluate_market_metrics",
    _ROOT / "skills/evaluate-market-metrics/evaluate_market_metrics.py",
)
assert _SPEC and _SPEC.loader
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)

_ERROR_DB_LOCKED = _mod._ERROR_DB_LOCKED
_ERROR_INSUFFICIENT_DATA = _mod._ERROR_INSUFFICIENT_DATA
compute_signal_bundle_from_series = _mod.compute_signal_bundle_from_series
evaluate_market_metrics = _mod.evaluate_market_metrics
merge_filters = _mod.merge_filters


def _snap(
    offset_hrs: float,
    *,
    midpoint: float,
    yes: float | None = None,
    no: float | None = None,
    volume: float = 1000.0,
    liquidity: float = 5000.0,
    spread: float = 0.02,
) -> dict:
    base = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    dt = base + timedelta(hours=offset_hrs)
    y = yes if yes is not None else midpoint - 0.01
    n = no if no is not None else 1.0 - y
    return {
        "datetime": dt.isoformat(),
        "yes_price": y,
        "no_price": n,
        "volume": volume,
        "liquidity": liquidity,
        "last_trade_price": midpoint,
        "midpoint": midpoint,
        "spread": spread,
    }


def _rising_series(n: int, start_mid: float = 0.40) -> list[dict]:
    return [
        _snap(float(i), midpoint=start_mid + i * 0.01, volume=1000 + i * 100)
        for i in range(n)
    ]


class TestMergeFilters:
    def test_overrides_merge(self):
        merged = merge_filters({"breakout_pct_shift": 0.12})
        assert merged["breakout_pct_shift"] == 0.12
        assert merged["arbitrage_max_combined_ask"] == FILTERS["arbitrage_max_combined_ask"]


class TestComputeSignalBundle:
    def test_arbitrage_hard_pass(self):
        series = [
            _snap(0, midpoint=0.48, yes=0.47, no=0.48),
            _snap(1, midpoint=0.48, yes=0.47, no=0.48),
        ]
        out = compute_signal_bundle_from_series(
            "0xarb",
            series,
            dict(FILTERS),
            days_since_creation=10.0,
            start_date_available=True,
        )
        assert out.error is None
        assert out.hard_veto.passed is True
        assert out.hard_veto.trigger == "arbitrage"

    def test_insufficient_data_hard_reject(self):
        out = compute_signal_bundle_from_series(
            "0xempty",
            [_snap(0, midpoint=0.5)],
            dict(FILTERS),
            days_since_creation=None,
            start_date_available=False,
        )
        assert out.error == _ERROR_INSUFFICIENT_DATA
        assert out.hard_veto.passed is False

    def test_breakout_start_end_prices(self):
        series = [
            _snap(0, midpoint=0.40),
            _snap(4, midpoint=0.50),
        ]
        out = compute_signal_bundle_from_series(
            "0xbo",
            series,
            dict(FILTERS),
            days_since_creation=5.0,
            start_date_available=True,
        )
        br = out.signals["breakout"]
        assert br["start_price"] is not None
        assert br["end_price"] == 0.50
        assert br["pct_move"] > 0

    def test_info_drift_net_pct_change(self):
        series = _rising_series(12, start_mid=0.40)
        out = compute_signal_bundle_from_series(
            "0xdrift",
            series,
            dict(FILTERS),
            days_since_creation=30.0,
            start_date_available=True,
        )
        drift = out.signals["info_drift"]
        assert drift["max_run"] >= 2
        assert drift["net_pct_change"] > 0

    def test_missing_start_date(self):
        series = [_snap(0, midpoint=0.5), _snap(1, midpoint=0.51)]
        out = compute_signal_bundle_from_series(
            "0xnostart",
            series,
            dict(FILTERS),
            days_since_creation=None,
            start_date_available=False,
        )
        assert out.latest_snapshot["days_since_creation"] is None
        assert out.data_quality["start_date_available"] is False

    def test_soft_signals_no_hard_veto(self):
        series = [_snap(0, midpoint=0.50), _snap(1, midpoint=0.51)]
        out = compute_signal_bundle_from_series(
            "0xsoft",
            series,
            dict(FILTERS),
            days_since_creation=20.0,
            start_date_available=True,
        )
        assert out.hard_veto.passed is None
        assert "volume_shock" in out.signals


class TestEvaluateMarketMetricsDB:
    def test_db_locked_on_timeout(self):
        with patch.object(_mod, "load_market_trends", return_value=([], _ERROR_DB_LOCKED)):
            out = evaluate_market_metrics("0xlocked")
        assert out.error == _ERROR_DB_LOCKED
        assert out.hard_veto.passed is None

    def test_insufficient_data_when_empty_trends(self):
        with patch.object(
            _mod,
            "load_market_metadata",
            return_value=({"start_date": "2026-01-01T00:00:00+00:00"}, None),
        ), patch.object(_mod, "load_market_trends", return_value=([], None)):
            out = evaluate_market_metrics("0xempty")
        assert out.error == _ERROR_INSUFFICIENT_DATA
