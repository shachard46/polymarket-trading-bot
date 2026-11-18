"""Deterministic Phase 2 gate: eligibility, ranking score, backoff, arbitrage.

Pure functions only (no vault, no agents, no network). The strategy is
research-edge, not momentum: the gate decides *which markets are worth an LLM's
attention*, never what the price will do. ``phases.phase2_quantitative_routing``
owns persistence and ledger writes.

A "market row" is the flat ``market_data`` dict (``yes_price``, ``liquidity``,
``days_to_resolution``, ...) or a dict wrapping it under ``market_data``.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from config.trading_constants import (
    FILTERS,
    LIQ_FRAC,
    MANIP,
    MIN_ORDER_USD,
)
from orchestrator.scraper import select_q

Q_MIN = FILTERS["gate_min_q"]  # defaults; the live values come from the directives `filters:` block
Q_MAX = FILTERS["gate_max_q"]
MIN_LIQUIDITY_USD = MIN_ORDER_USD / LIQ_FRAC

# Backoff schedule (docs/02): 5h * 2^(fail_count-1), capped at 7d; retire after 6 fails.
BACKOFF_BASE_HOURS = 5.0
BACKOFF_CAP_HOURS = 7 * 24.0
MAX_FAILS = 6
PRICE_MOVE_RESET = 0.05

STATUS_RETIRED = "retired"
_CLOSED_STATUSES = frozenset({"closed", "resolved", "archived"})

SWEET_SPOT_DAYS = (14.0, 45.0)
_TURNOVER_HALF = 20.0  # volume/liquidity at which the attention term is 0.5


def _data(row: Mapping[str, Any] | None) -> Mapping[str, Any]:
    row = row or {}
    inner = row.get("market_data")
    return inner if isinstance(inner, Mapping) else row


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _knob(knobs: Mapping[str, Any] | None, name: str) -> float:
    """Directive override (``extract_filter_directives`` output) else the constant default."""
    value = (knobs or {}).get(name)
    return FILTERS[name] if value is None else value


def eligibility(
    market_row: Mapping[str, Any] | None,
    knobs: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    """Return ``(ok, reason)``; ``reason`` is ``"ok"`` or a short rejection code.

    Codes: ``closed``, ``no_price``, ``price_extreme`` (q outside
    [``gate_min_q``, ``gate_max_q``], default [0.05, 0.95]), ``no_end_date``, ``too_soon``
    (< ``min_days_to_resolution``), ``too_far`` (> ``max_days_to_resolution``), ``illiquid``
    (liquidity < MIN_ORDER_USD / LIQ_FRAC or missing), ``suspicious_move`` (|day_price_change| >=
    MANIP threshold on thin 24h volume vs liquidity), ``concentrated`` (``holder_top1``/``holder_top3``
    share above MANIP thresholds), ``wide_spread`` (yes_ask - yes_bid > MANIP max). Each of the last
    three is skipped when its signal is missing.

    ``knobs`` is the directives-derived threshold mapping (see
    ``orchestrator.directives.extract_filter_directives``); missing keys fall back to the constants.
    """
    md = _data(market_row)
    row = market_row or {}
    status = str(md.get("status") or row.get("status") or "").strip().lower()
    if status in _CLOSED_STATUSES or md.get("closed") is True or row.get("closed") is True:
        return False, "closed"
    q = select_q(dict(md))
    if q is None:
        return False, "no_price"
    if not (_knob(knobs, "gate_min_q") <= q <= _knob(knobs, "gate_max_q")):
        return False, "price_extreme"
    days = _num(md.get("days_to_resolution"))
    if days is None:
        return False, "no_end_date"
    if days < _knob(knobs, "min_days_to_resolution"):
        return False, "too_soon"
    if days > _knob(knobs, "max_days_to_resolution"):
        return False, "too_far"
    liq = _num(md.get("liquidity"))
    if liq is None or liq < MIN_LIQUIDITY_USD:
        return False, "illiquid"
    chg, vol24 = _num(md.get("day_price_change")), _num(md.get("volume_24h"))
    if (
        chg is not None
        and vol24 is not None
        and abs(chg) >= MANIP["suspicious_move_min_abs_change"]
        and vol24 < MANIP["suspicious_move_max_vol24_frac"] * liq
    ):
        return False, "suspicious_move"
    top1, top3 = _num(md.get("holder_top1")), _num(md.get("holder_top3"))
    if (top1 is not None and top1 >= MANIP["concentrated_top1_share"]) or (
        top3 is not None and top3 >= MANIP["concentrated_top3_share"]
    ):
        return False, "concentrated"
    bid, ask = _num(md.get("yes_bid")), _num(md.get("yes_ask"))
    if bid is not None and ask is not None and ask - bid > MANIP["wide_spread_max"]:
        return False, "wide_spread"
    return True, "ok"


def score(
    market_row: Mapping[str, Any] | None,
    knobs: Mapping[str, Any] | None = None,
) -> float:
    """Ranking score for an *eligible* market (higher = look at it first).

    ``score = 0.5*attention + 0.3*time + 0.2*price``

    - attention = 1 / (1 + turnover / 20), turnover = volume / liquidity. Low
      attention (thinly traded relative to depth) means the crowd has not yet
      priced in public information, which is where research can have an edge.
    - time = 1.0 for 14-45 days to resolution; linear ramp 0.4 -> 1.0 from 7 to
      14 days and 1.0 -> 0.4 from 45 to 60 days.
    - price = 1 - 2*|q - 0.5| (1.0 at 50c, 0.1 at the 5c/95c edge).
    """
    md = _data(market_row)
    q = select_q(dict(md))
    q = 0.5 if q is None else q
    vol = _num(md.get("volume")) or 0.0
    liq = _num(md.get("liquidity")) or 0.0
    turnover = vol / liq if liq > 0 else 0.0
    attention = 1.0 / (1.0 + max(turnover, 0.0) / _TURNOVER_HALF)

    days = _num(md.get("days_to_resolution"))
    min_days = _knob(knobs, "min_days_to_resolution")
    max_days = _knob(knobs, "max_days_to_resolution")
    lo = max(SWEET_SPOT_DAYS[0], min_days + 1.0)  # keep the ramps non-degenerate if the window moves
    hi = min(SWEET_SPOT_DAYS[1], max_days - 1.0)
    if days is None:
        time_term = 0.4
    elif lo <= days <= hi:
        time_term = 1.0
    elif days < lo:
        time_term = 0.4 + 0.6 * max(days - min_days, 0.0) / (lo - min_days)
    else:
        time_term = 1.0 - 0.6 * min(days - hi, max_days - hi) / (max_days - hi)

    price_term = 1.0 - 2.0 * abs(q - 0.5)
    return round(0.5 * attention + 0.3 * time_term + 0.2 * price_term, 6)


# ---------------------------------------------------------------------------
# Arbitrage
# ---------------------------------------------------------------------------


def arbitrage_combined(market_row: Mapping[str, Any] | None) -> float | None:
    """``yes_ask + no_ask`` when it signals an arbitrage (< threshold), else ``None``.

    Uses the API's ``yes_ask``/``no_ask`` when both are present, else ``yes_price``/``no_price``.
    Note: Gamma only quotes the YES token, so a row's ``no_ask`` is ``1 - yes_bid`` and the sum
    is >= 1; the check can only fire on rows carrying a real NO-book ask.
    """
    md = _data(market_row)
    yes, no = _num(md.get("yes_ask")), _num(md.get("no_ask"))
    if yes is None or no is None:
        yes, no = _num(md.get("yes_price")), _num(md.get("no_price"))
    if yes is None or no is None or not (0.0 < yes < 1.0 and 0.0 < no < 1.0):
        return None
    combined = yes + no
    if combined < FILTERS["arbitrage_max_combined_ask"]:
        return round(combined, 6)
    return None


# ---------------------------------------------------------------------------
# Backoff state (persisted in 01_Filters/{id}.md frontmatter)
# ---------------------------------------------------------------------------


def backoff_delay(fail_count: int) -> timedelta:
    """5h * 2^(fail_count-1), capped at 7 days. ``fail_count`` < 1 -> no delay."""
    if fail_count < 1:
        return timedelta(0)
    hours = min(BACKOFF_BASE_HOURS * 2 ** (fail_count - 1), BACKOFF_CAP_HOURS)
    return timedelta(hours=hours)


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_retired(record: Mapping[str, Any] | None) -> bool:
    return str((record or {}).get("status") or "").strip().lower() == STATUS_RETIRED


def is_due(record: Mapping[str, Any] | None, now: datetime | None = None) -> bool:
    """True when the market has no backoff pending (never checked, or next_check_at passed)."""
    if not record:
        return True
    nxt = _parse_ts(record.get("next_check_at"))
    if nxt is None:
        return True
    return (now or datetime.now(timezone.utc)) >= nxt


def register_failure(
    record: Mapping[str, Any] | None,
    q: float | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """New backoff fields after a failed check (gate reject).

    ``fail_count`` first resets to 0 if the price moved more than 0.05 since
    ``last_q`` (the situation changed, so re-earn the full schedule), then
    increments. At ``MAX_FAILS`` consecutive failures the market is retired.
    """
    now = now or datetime.now(timezone.utc)
    rec = record or {}
    try:
        fails = int(rec.get("fail_count") or 0)
    except (TypeError, ValueError):
        fails = 0
    last_q = _num(rec.get("last_q"))
    if q is not None and last_q is not None and abs(q - last_q) > PRICE_MOVE_RESET:
        fails = 0
    fails += 1
    out: dict[str, Any] = {
        "fail_count": fails,
        "next_check_at": (now + backoff_delay(fails)).isoformat(),
        "last_q": q if q is not None else last_q,
    }
    if fails >= MAX_FAILS:
        out["status"] = STATUS_RETIRED
    return out


def register_pass(q: float | None) -> dict[str, Any]:
    """Backoff fields after a passed check: failures are consecutive, so reset."""
    return {"fail_count": 0, "next_check_at": None, "last_q": q}


__all__ = [
    "BACKOFF_BASE_HOURS",
    "BACKOFF_CAP_HOURS",
    "MAX_FAILS",
    "MIN_LIQUIDITY_USD",
    "STATUS_RETIRED",
    "arbitrage_combined",
    "backoff_delay",
    "eligibility",
    "is_due",
    "is_retired",
    "register_failure",
    "register_pass",
    "score",
]
