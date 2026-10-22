"""Hardening tests for evaluate_market_metrics (override coercion, arbitrage, scanner errors)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

from config.trading_constants import FILTERS

_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "evaluate_market_metrics_h",
    _ROOT / "skills/evaluate-market-metrics/evaluate_market_metrics.py",
)
assert _SPEC and _SPEC.loader
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)


def test_merge_filters_coerces_and_rejects_invalid():
    merged = _mod.merge_filters(
        {
            "breakout_pct_shift": "0.12",
            "volume_shock_ma_multiplier": -1,
            "spread_anomaly_multiplier": "abc",
            "info_drift_sequential_trades": 11.0,
        }
    )
    assert merged["breakout_pct_shift"] == 0.12
    assert merged["volume_shock_ma_multiplier"] == FILTERS["volume_shock_ma_multiplier"]
    assert merged["spread_anomaly_multiplier"] == FILTERS["spread_anomaly_multiplier"]
    assert merged["info_drift_sequential_trades"] == 11


def test_arbitrage_requires_valid_prices():
    f = dict(FILTERS)
    assert _mod._check_arbitrage_hard_veto({"yes_price": 0.4, "no_price": 0.5}, f) is not None
    for bad in (None, 0, 1.0, "0.4", float("nan")):
        assert _mod._check_arbitrage_hard_veto({"yes_price": bad, "no_price": 0.5}, f) is None


def test_missing_binary_is_scanner_error():
    with patch.object(_mod.shutil, "which", return_value=None):
        _, err = _mod._run_poly_scan("get_market", "x")
    assert err == "SCANNER_ERROR"


def test_unexpected_exception_returns_structured_error():
    with patch.object(_mod, "load_market_metadata", side_effect=RuntimeError("x")):
        out = _mod.evaluate_market_metrics("m")
    assert out.error == "SCANNER_ERROR"
    assert out.hard_veto.passed is None


def test_volume_shock_zero_baseline_does_not_divide():
    series = [
        {"datetime": f"2026-01-01T0{i}:00:00+00:00", "volume": 0, "yes_price": 0.4, "no_price": 0.6}
        for i in range(3)
    ]
    series[-1]["volume"] = 10
    out = _mod.compute_signal_bundle_from_series(
        "m", series, dict(FILTERS), days_since_creation=1.0, start_date_available=True
    )
    vs = out.signals["volume_shock"]
    assert vs["ratio"] is None and vs["fired"] is True
