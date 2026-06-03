"""evaluate_market_metrics — OpenClaw skill execution module.

Contract: docs/04_skills_contracts.md §1

Loads market history via poly-scan, computes signal features, returns signal_bundle.
Pass/fail for soft filters is agent-judged; only arbitrage and insufficient data are hard_veto.
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from config.trading_constants import FILTERS

_ERROR_DB_LOCKED = "DB_LOCKED"
_ERROR_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
_MIN_SNAPSHOTS = 2

_DEFAULT_POLY_SCAN_BIN = "poly-scan"
_DEFAULT_TIMEOUT_SEC = 60.0


class EvaluateMarketMetricsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market_id: str
    filter_overrides: dict[str, Any] | None = None


class HardVeto(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool | None
    trigger: str | None = None
    details: str | None = None


class EvaluateMarketMetricsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market_id: str
    latest_snapshot: dict[str, Any] = Field(default_factory=dict)
    hard_veto: HardVeto
    signals: dict[str, Any] = Field(default_factory=dict)
    thresholds_applied: dict[str, Any] = Field(default_factory=dict)
    data_quality: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


# ---------------------------------------------------------------------------
# DB / poly-scan helpers
# ---------------------------------------------------------------------------


def _poly_scan_bin() -> str:
    return os.environ.get("POLY_SCAN_BIN", _DEFAULT_POLY_SCAN_BIN)


def _timeout_sec() -> float:
    raw = os.environ.get("POLY_SCAN_TIMEOUT_SEC")
    if not raw:
        return _DEFAULT_TIMEOUT_SEC
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_SEC


def _trends_limit(filters: dict[str, Any]) -> int:
    hrs = max(
        int(filters["breakout_time_window_hrs"]),
        int(filters["low_liquidity_dead_window_hrs"]),
    )
    return max(hrs * 4, 200)


def _run_poly_scan(*args: str) -> tuple[Any | None, str | None]:
    """Run poly-scan; return (parsed_json, error_code)."""
    binary = _poly_scan_bin()
    if shutil.which(binary) is None:
        return None, _ERROR_DB_LOCKED

    cmd = [binary, *args]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_timeout_sec(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, _ERROR_DB_LOCKED
    except OSError:
        return None, _ERROR_DB_LOCKED

    stderr = (completed.stderr or "").strip().lower()
    if "database is locked" in stderr or "database is locked" in (completed.stdout or "").lower():
        return None, _ERROR_DB_LOCKED

    if completed.returncode != 0:
        if "locked" in stderr or "busy" in stderr:
            return None, _ERROR_DB_LOCKED
        return None, _ERROR_DB_LOCKED

    try:
        return json.loads(completed.stdout), None
    except json.JSONDecodeError:
        return None, _ERROR_DB_LOCKED


def load_market_trends(market_id: str, limit: int) -> tuple[list[dict[str, Any]], str | None]:
    """Return oldest-first trend rows or ([], error_code)."""
    data, err = _run_poly_scan("get_market_trends", market_id, "--limit", str(limit))
    if err:
        return [], err
    if not isinstance(data, list):
        return [], _ERROR_INSUFFICIENT_DATA
    return list(reversed(data)), None


def load_market_metadata(market_id: str) -> tuple[dict[str, Any], str | None]:
    """Return market dict from get_market or ({}, error_code)."""
    data, err = _run_poly_scan("get_market", market_id)
    if err:
        return {}, err
    if not isinstance(data, dict):
        return {}, _ERROR_DB_LOCKED
    return data, None


def merge_filters(filter_overrides: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(FILTERS)
    if filter_overrides:
        merged.update(filter_overrides)
    return merged


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_dt(raw: str | datetime) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(str(raw))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# Price fields in priority order. The scanner's book-derived ``midpoint`` is
# unreliable on illiquid books (it pins to 0.5 with a ~1.0 spread when the BBO
# only has far-out resting orders), so the canonical display price ``yes_price``
# — and ``last_trade_price`` as a final fallback — drive the movement signals.
_PRICE_FIELDS: tuple[str, ...] = ("yes_price", "last_trade_price", "midpoint")


def _price(snap: dict[str, Any]) -> float | None:
    """Return the most reliable traded price for a snapshot, or ``None``."""
    for field in _PRICE_FIELDS:
        value = snap.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _snapshot_near_hours_ago(series: list[dict], hours: float) -> dict | None:
    latest_dt = _parse_dt(series[-1]["datetime"])
    target_dt = latest_dt.timestamp() - hours * 3600
    best = None
    best_delta = float("inf")
    for snap in series[:-1]:
        delta = abs(_parse_dt(snap["datetime"]).timestamp() - target_dt)
        if delta < best_delta:
            best_delta = delta
            best = snap
    return best


def _days_since_creation(start_date_raw: Any) -> float | None:
    if start_date_raw is None:
        return None
    try:
        start = _parse_dt(start_date_raw)
    except (TypeError, ValueError):
        return None
    now = datetime.now(tz=timezone.utc)
    return (now - start).total_seconds() / 86400.0


def _build_latest_snapshot(
    latest: dict[str, Any],
    *,
    days_since_creation: float | None,
) -> dict[str, Any]:
    total_volume = latest.get("volume")
    return {
        "datetime": latest.get("datetime"),
        "midpoint": latest.get("midpoint"),
        "volume": latest.get("volume"),
        "liquidity": latest.get("liquidity"),
        "spread": latest.get("spread"),
        "yes_price": latest.get("yes_price"),
        "no_price": latest.get("no_price"),
        "days_since_creation": days_since_creation,
        "total_volume": total_volume,
    }


def _compute_info_drift_metrics(
    series: list[dict],
    threshold: int,
) -> dict[str, Any]:
    mids = [p for s in series if (p := _price(s)) is not None]
    if len(mids) < 2:
        return {
            "max_run": 0,
            "threshold": threshold,
            "direction": None,
            "net_pct_change": 0.0,
            "proxy": "snapshot",
        }

    max_run = 1
    current_run = 1
    direction: str | None = None
    run_start_idx = 0

    best_run = 1
    best_direction: str | None = None
    best_start_idx = 0

    for i in range(1, len(mids)):
        if mids[i] > mids[i - 1]:
            dir_i = 1
        elif mids[i] < mids[i - 1]:
            dir_i = -1
        else:
            current_run = 1
            run_start_idx = i
            continue

        if i >= 2:
            prev_dir = 1 if mids[i - 1] > mids[i - 2] else -1 if mids[i - 1] < mids[i - 2] else 0
        else:
            prev_dir = 0

        if dir_i == prev_dir and prev_dir != 0:
            current_run += 1
        else:
            current_run = 1
            run_start_idx = i - 1

        if current_run > best_run:
            best_run = current_run
            best_direction = "up" if dir_i > 0 else "down"
            best_start_idx = run_start_idx

    max_run = best_run
    direction = best_direction
    start_mid = mids[best_start_idx] if best_start_idx < len(mids) else mids[0]
    end_mid = mids[-1]
    if start_mid and start_mid != 0:
        net_pct_change = abs(end_mid - start_mid) / abs(start_mid)
    else:
        net_pct_change = 0.0

    return {
        "max_run": max_run,
        "threshold": threshold,
        "direction": direction,
        "net_pct_change": round(net_pct_change, 6),
        "proxy": "snapshot",
    }


def _compute_signals_from_series(
    series: list[dict[str, Any]],
    filters: dict[str, Any],
    *,
    days_since_creation: float | None,
) -> dict[str, Any]:
    latest = series[-1]

    # volume_shock
    volumes = [s["volume"] for s in series[:-1] if s.get("volume") is not None]
    if volumes:
        baseline_vol = statistics.median(volumes)
        current_vol = latest.get("volume") or 0
        vol_threshold = baseline_vol * filters["volume_shock_ma_multiplier"]
        vol_ratio = current_vol / baseline_vol if baseline_vol else 0.0
    else:
        baseline_vol = None
        current_vol = latest.get("volume")
        vol_threshold = filters["volume_shock_ma_multiplier"]
        vol_ratio = 0.0

    # breakout
    window_hrs = filters["breakout_time_window_hrs"]
    ref = _snapshot_near_hours_ago(series, window_hrs)
    ref_price = _price(ref) if ref else None
    latest_price = _price(latest)
    if ref_price:
        start_price = ref_price
        end_price = latest_price if latest_price is not None else start_price
        pct_move = abs(end_price - start_price) / start_price
    else:
        start_price = None
        end_price = latest_price
        pct_move = 0.0

    # spread_anomaly
    spreads = [s["spread"] for s in series[:-1] if s.get("spread") is not None]
    if spreads:
        baseline_spread = statistics.median(spreads)
        current_spread = latest.get("spread") or 0
        spread_threshold = baseline_spread * filters["spread_anomaly_multiplier"]
        spread_ratio = current_spread / baseline_spread if baseline_spread else 0.0
    else:
        baseline_spread = None
        current_spread = latest.get("spread")
        spread_threshold = filters["spread_anomaly_multiplier"]
        spread_ratio = 0.0

    # low_liquidity_breakout
    liq = latest.get("liquidity")
    dead_hrs = filters["low_liquidity_dead_window_hrs"]
    ref_liq = _snapshot_near_hours_ago(series, dead_hrs)
    ref_liq_price = _price(ref_liq) if ref_liq else None
    if (
        liq is not None
        and liq < filters["low_liquidity_breakout_max_liq"]
        and ref_liq_price
    ):
        liq_pct = abs((latest_price or 0) - ref_liq_price) / ref_liq_price
        liq_fired = liq_pct > filters["low_liquidity_breakout_pct"]
    else:
        liq_pct = 0.0
        liq_fired = False

    info_drift = _compute_info_drift_metrics(
        series, int(filters["info_drift_sequential_trades"])
    )

    return {
        "volume_shock": {
            "ratio": round(vol_ratio, 4),
            "threshold": filters["volume_shock_ma_multiplier"],
            "baseline_median": baseline_vol,
            "current": current_vol,
        },
        "breakout": {
            "pct_move": round(pct_move, 6),
            "threshold": filters["breakout_pct_shift"],
            "window_hrs": window_hrs,
            "start_price": start_price,
            "end_price": end_price,
        },
        "spread_anomaly": {
            "ratio": round(spread_ratio, 4),
            "threshold": filters["spread_anomaly_multiplier"],
            "baseline_median": baseline_spread,
            "current": current_spread,
        },
        "low_liquidity_breakout": {
            "liquidity": liq,
            "pct_move": round(liq_pct, 6),
            "threshold": filters["low_liquidity_breakout_pct"],
            "fired": liq_fired,
        },
        "info_drift": info_drift,
    }


def _check_arbitrage_hard_veto(latest: dict, filters: dict[str, Any]) -> HardVeto | None:
    combined = latest["yes_price"] + latest["no_price"]
    threshold = filters["arbitrage_max_combined_ask"]
    if combined < threshold:
        return HardVeto(
            passed=True,
            trigger="arbitrage",
            details=f"arbitrage: yes_price+no_price={combined:.4f} < threshold {threshold}",
        )
    return None


def _empty_output(market_id: str, error: str) -> EvaluateMarketMetricsOutput:
    return EvaluateMarketMetricsOutput(
        market_id=market_id,
        hard_veto=HardVeto(passed=None, trigger=None, details=None),
        error=error,
    )


def compute_signal_bundle_from_series(
    market_id: str,
    series: list[dict[str, Any]],
    filters: dict[str, Any],
    *,
    days_since_creation: float | None,
    start_date_available: bool,
) -> EvaluateMarketMetricsOutput:
    """Testable path: build signal bundle from pre-loaded oldest-first series."""
    if len(series) < _MIN_SNAPSHOTS:
        return EvaluateMarketMetricsOutput(
            market_id=market_id,
            latest_snapshot={},
            hard_veto=HardVeto(
                passed=False,
                trigger=None,
                details=f"insufficient data: need ≥{_MIN_SNAPSHOTS} snapshots, have {len(series)}",
            ),
            thresholds_applied=filters,
            data_quality={
                "snapshots_used": len(series),
                "oldest": series[0].get("datetime") if series else None,
                "newest": series[-1].get("datetime") if series else None,
                "start_date_available": start_date_available,
            },
            error=_ERROR_INSUFFICIENT_DATA,
        )

    series = sorted(series, key=lambda s: _parse_dt(s["datetime"]))
    latest = series[-1]
    latest_snapshot = _build_latest_snapshot(
        latest, days_since_creation=days_since_creation
    )

    arb = _check_arbitrage_hard_veto(latest, filters)
    signals = _compute_signals_from_series(
        series, filters, days_since_creation=days_since_creation
    )
    data_quality = {
        "snapshots_used": len(series),
        "oldest": series[0].get("datetime"),
        "newest": series[-1].get("datetime"),
        "start_date_available": start_date_available,
    }

    if arb is not None:
        return EvaluateMarketMetricsOutput(
            market_id=market_id,
            latest_snapshot=latest_snapshot,
            hard_veto=arb,
            signals=signals,
            thresholds_applied=filters,
            data_quality=data_quality,
            error=None,
        )

    return EvaluateMarketMetricsOutput(
        market_id=market_id,
        latest_snapshot=latest_snapshot,
        hard_veto=HardVeto(passed=None, trigger=None, details=None),
        signals=signals,
        thresholds_applied=filters,
        data_quality=data_quality,
        error=None,
    )


def evaluate_market_metrics(
    market_id: str,
    filter_overrides: dict[str, Any] | None = None,
) -> EvaluateMarketMetricsOutput:
    filters = merge_filters(filter_overrides)
    limit = _trends_limit(filters)

    meta, meta_err = load_market_metadata(market_id)
    start_date = meta.get("start_date") if meta else None
    days_since = _days_since_creation(start_date) if not meta_err else None
    start_date_available = start_date is not None and days_since is not None

    series, trends_err = load_market_trends(market_id, limit)
    if trends_err == _ERROR_DB_LOCKED:
        return _empty_output(market_id, _ERROR_DB_LOCKED)
    if trends_err or not series:
        return EvaluateMarketMetricsOutput(
            market_id=market_id,
            hard_veto=HardVeto(
                passed=False,
                trigger=None,
                details="no trend data available",
            ),
            thresholds_applied=filters,
            data_quality={
                "snapshots_used": 0,
                "oldest": None,
                "newest": None,
                "start_date_available": start_date_available,
            },
            error=_ERROR_INSUFFICIENT_DATA,
        )

    return compute_signal_bundle_from_series(
        market_id,
        series,
        filters,
        days_since_creation=days_since,
        start_date_available=start_date_available,
    )


# Pydantic 2.13+ defers resolving ``from __future__ import annotations`` until rebuild.
EvaluateMarketMetricsInput.model_rebuild()
HardVeto.model_rebuild()
EvaluateMarketMetricsOutput.model_rebuild()


__all__ = [
    "EvaluateMarketMetricsInput",
    "EvaluateMarketMetricsOutput",
    "HardVeto",
    "compute_signal_bundle_from_series",
    "evaluate_market_metrics",
    "load_market_metadata",
    "load_market_trends",
    "merge_filters",
]
