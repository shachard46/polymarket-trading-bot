"""evaluate_market_metrics — OpenClaw skill execution module.

Contract: docs/04_skills_contracts.md §1

Input:  historic_market_data — chronological list of market snapshots (oldest first).
        Each snapshot is a dict with keys: datetime, yes_price, no_price, volume,
        liquidity, last_trade_price, midpoint, spread.
        The orchestrator sources this from poly-scan get_market_trends (which returns
        newest-first — reverse before calling this skill).

Output: passed, trigger, confidence_multiplier, details, error.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict

from config.trading_constants import FILTERS


class EvaluateMarketMetricsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    historic_market_data: list[dict[str, Any]]


class EvaluateMarketMetricsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    trigger: str | None
    confidence_multiplier: float
    details: str
    error: str | None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_dt(raw: str | datetime) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(str(raw))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _hours_between(a: dict, b: dict) -> float:
    return abs((_parse_dt(b["datetime"]) - _parse_dt(a["datetime"])).total_seconds()) / 3600.0


def _snapshot_near_hours_ago(series: list[dict], hours: float) -> dict | None:
    """Return the snapshot closest to `hours` before the latest snapshot."""
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


# ---------------------------------------------------------------------------
# Six filters (first fire wins)
# ---------------------------------------------------------------------------

def _check_arbitrage(latest: dict) -> tuple[bool, str]:
    combined = latest["yes_price"] + latest["no_price"]
    threshold = FILTERS["arbitrage_max_combined_ask"]
    if combined < threshold:
        return True, (
            f"arbitrage: yes_price+no_price={combined:.4f} < threshold {threshold}"
        )
    return False, ""


def _check_volume_shock(series: list[dict]) -> tuple[bool, str]:
    if len(series) < 2:
        return False, "volume_shock skipped: insufficient history"
    volumes = [s["volume"] for s in series[:-1] if s["volume"] is not None]
    if not volumes:
        return False, "volume_shock skipped: no baseline volume data"
    baseline = statistics.median(volumes)
    current = series[-1]["volume"]
    threshold = baseline * FILTERS["volume_shock_ma_multiplier"]
    if current > threshold:
        return True, (
            f"volume_shock: current={current:.2f} > "
            f"baseline_median={baseline:.2f} × {FILTERS['volume_shock_ma_multiplier']}"
        )
    return False, ""


def _check_breakout(series: list[dict]) -> tuple[bool, str]:
    window_hrs = FILTERS["breakout_time_window_hrs"]
    ref = _snapshot_near_hours_ago(series, window_hrs)
    if ref is None:
        return False, "breakout skipped: no snapshot near breakout_time_window_hrs ago"
    base_mid = ref["midpoint"]
    if base_mid is None or base_mid == 0:
        return False, "breakout skipped: baseline midpoint is zero or None"
    pct = abs(series[-1]["midpoint"] - base_mid) / base_mid
    if pct > FILTERS["breakout_pct_shift"]:
        return True, (
            f"breakout: midpoint moved {pct:.2%} over ~{window_hrs}h "
            f"(threshold {FILTERS['breakout_pct_shift']:.2%})"
        )
    return False, ""


def _check_spread_anomaly(series: list[dict]) -> tuple[bool, str]:
    if len(series) < 2:
        return False, "spread_anomaly skipped: insufficient history"
    spreads = [s["spread"] for s in series[:-1] if s["spread"] is not None]
    if not spreads:
        return False, "spread_anomaly skipped: no baseline spread data"
    baseline = statistics.median(spreads)
    if baseline == 0:
        return False, "spread_anomaly skipped: baseline spread is zero"
    current = series[-1]["spread"]
    threshold = baseline * FILTERS["spread_anomaly_multiplier"]
    if current > threshold:
        return True, (
            f"spread_anomaly: current={current:.4f} > "
            f"baseline_median={baseline:.4f} × {FILTERS['spread_anomaly_multiplier']}"
        )
    return False, ""


def _check_low_liquidity_breakout(series: list[dict]) -> tuple[bool, str]:
    latest = series[-1]
    if latest["liquidity"] is None or latest["liquidity"] >= FILTERS["low_liquidity_breakout_max_liq"]:
        return False, ""
    window_hrs = FILTERS["low_liquidity_dead_window_hrs"]
    ref = _snapshot_near_hours_ago(series, window_hrs)
    if ref is None:
        return False, "low_liquidity_breakout skipped: no snapshot near dead_window_hrs ago"
    base_mid = ref["midpoint"]
    if base_mid is None or base_mid == 0:
        return False, "low_liquidity_breakout skipped: baseline midpoint is zero or None"
    pct = abs(latest["midpoint"] - base_mid) / base_mid
    if pct > FILTERS["low_liquidity_breakout_pct"]:
        return True, (
            f"low_liquidity_breakout: liquidity={latest['liquidity']:.2f} < "
            f"{FILTERS['low_liquidity_breakout_max_liq']}, "
            f"midpoint moved {pct:.2%} (threshold {FILTERS['low_liquidity_breakout_pct']:.2%})"
        )
    return False, ""


def _check_info_drift(series: list[dict]) -> tuple[bool, str]:
    """Snapshot-proxy for info drift.

    Without a CLOB trade tape, we count consecutive midpoint moves in the same
    direction across chronological snapshots. This is a snapshot-level approximation;
    replace with trade-level event data when available.
    """
    threshold = FILTERS["info_drift_sequential_trades"]
    if len(series) < threshold + 1:
        return False, (
            f"info_drift skipped: need ≥{threshold + 1} snapshots, "
            f"have {len(series)} (snapshot-proxy)"
        )
    mids = [s["midpoint"] for s in series if s["midpoint"] is not None]
    if len(mids) < 2:
        return False, "info_drift skipped: insufficient midpoint data"

    max_run = 1
    current_run = 1
    for i in range(1, len(mids)):
        if mids[i] > mids[i - 1]:
            direction = 1
        elif mids[i] < mids[i - 1]:
            direction = -1
        else:
            current_run = 1
            continue
        prev_dir = 1 if mids[i - 1] > mids[i - 2] else -1 if i >= 2 and mids[i - 1] < mids[i - 2] else 0
        if direction == prev_dir:
            current_run += 1
            max_run = max(max_run, current_run)
        else:
            current_run = 1

    if max_run >= threshold:
        return True, (
            f"info_drift: {max_run} consecutive same-direction midpoint moves "
            f"(threshold {threshold}, snapshot-proxy)"
        )
    return False, ""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_FILTERS_ORDERED = [
    ("arbitrage", lambda s: _check_arbitrage(s[-1])),
    ("volume_shock", _check_volume_shock),
    ("breakout", _check_breakout),
    ("spread_anomaly", _check_spread_anomaly),
    ("low_liquidity_breakout", _check_low_liquidity_breakout),
    ("info_drift", _check_info_drift),
]


def evaluate_market_metrics(
    historic_market_data: list[dict[str, Any]],
) -> EvaluateMarketMetricsOutput:
    try:
        if not historic_market_data:
            return EvaluateMarketMetricsOutput(
                passed=False,
                trigger=None,
                confidence_multiplier=1.0,
                details="No market data provided.",
                error=None,
            )

        series = sorted(historic_market_data, key=lambda s: _parse_dt(s["datetime"]))

        skipped: list[str] = []
        for name, check_fn in _FILTERS_ORDERED:
            fired, detail = check_fn(series)
            if fired:
                return EvaluateMarketMetricsOutput(
                    passed=True,
                    trigger=name,
                    confidence_multiplier=1.0,
                    details=detail,
                    error=None,
                )
            if detail:
                skipped.append(detail)

        skip_summary = "; ".join(skipped) if skipped else "all filters evaluated"
        return EvaluateMarketMetricsOutput(
            passed=False,
            trigger=None,
            confidence_multiplier=1.0,
            details=f"No filter triggered. Notes: {skip_summary}",
            error=None,
        )
    except Exception as exc:
        return EvaluateMarketMetricsOutput(
            passed=False,
            trigger=None,
            confidence_multiplier=1.0,
            details="",
            error=str(exc),
        )
