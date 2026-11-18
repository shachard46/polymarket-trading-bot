"""Research freshness state: TTL, cooldown, refresh triggers and attempt bookkeeping."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from obsidian_utils import ObsidianManager
from config.trading_constants import RESEARCH_MIN_TTL_HOURS, RESEARCH_REFRESH_PRICE_MOVE
from orchestrator import gate
from orchestrator.phases.common import (
    _as_float,
    _ledger_market_fields,
    _ledger_safe,
    _write_filter_state,
    log,
)
from orchestrator.state import is_error_free_active


def research_ttl_hours(days_to_resolution: float | None) -> float:
    """Refresh TTL: ``max(48h, days_to_resolution * 24h / 10)``."""
    days = days_to_resolution if days_to_resolution is not None else 0.0
    return max(RESEARCH_MIN_TTL_HOURS, float(days) * 24.0 / 10.0)


def _days_left(fm: dict[str, Any], now: datetime) -> float | None:
    end = gate._parse_ts(fm.get("end_date"))
    if end is not None:
        return max((end - now).total_seconds() / 86400.0, 0.0)
    return _as_float(fm.get("days_at_research"))


def _research_expired(
    fm: dict[str, Any], now: datetime, fallback_at: datetime | None = None
) -> bool:
    at = gate._parse_ts(fm.get("researched_at")) or fallback_at
    if at is None:
        return False
    return now - at >= timedelta(hours=research_ttl_hours(_days_left(fm, now)))


def _attempt_cooldown_active(state: Any, q: float | None, now: datetime) -> bool:
    """A recent research attempt (``research_state``) is still valid: not expired, q unmoved."""
    if not isinstance(state, dict):
        return False
    at = gate._parse_ts(state.get("at"))
    if at is None:
        return False
    ttl = _as_float(state.get("ttl_hours")) or RESEARCH_MIN_TTL_HOURS
    if now - at >= timedelta(hours=ttl):
        return False
    state_q = _as_float(state.get("q"))
    if q is not None and state_q is not None and abs(q - state_q) > RESEARCH_REFRESH_PRICE_MOVE:
        return False
    return True


def _refresh_reason(
    vault: ObsidianManager,
    market_id: str,
    fm: dict[str, Any],
    state: Any,
    q: float | None,
    now: datetime,
) -> str | None:
    """``"price_move"`` / ``"ttl"`` when error-free research is due for a refresh, else ``None``.

    ``q=None`` checks the TTL only (Phase 3 queue scan); Phase 2 passes the fresh q.
    A refresh attempt that already ran since the research (and changed nothing
    material) blocks another one until its own cooldown lapses.
    """
    q_then = _as_float(fm.get("q_at_research"))
    moved = (
        q is not None
        and q_then is not None
        and abs(q - q_then) > RESEARCH_REFRESH_PRICE_MOVE
    )
    path = vault.active_research_path(market_id)
    try:
        fallback = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        fallback = None
    expired = _research_expired(fm, now, fallback)
    if not (moved or expired):
        return None
    attempt_at = gate._parse_ts((state or {}).get("at")) if isinstance(state, dict) else None
    researched_at = gate._parse_ts(fm.get("researched_at")) or fallback
    if (
        attempt_at is not None
        and researched_at is not None
        and attempt_at > researched_at
        and _attempt_cooldown_active(state, q, now)
    ):
        return None
    return "price_move" if moved else "ttl"


def _error_free_active_fm(vault: ObsidianManager, market_id: str) -> dict[str, Any] | None:
    if not is_error_free_active(vault, market_id):
        return None
    return vault.read_market_record(market_id, "active")


def _record_attempt(
    vault: ObsidianManager,
    row: dict[str, Any],
    stage: str,
    outcome: str,
    **values: Any,
) -> None:
    """Persist the latest research attempt on the filter record (cooldown + escalation memory)."""
    market_id = row["market_id"]
    prior = vault.read_filter_log(market_id)
    if prior is None:
        return
    md = row.get("market_data") or {}
    state = {
        "stage": stage,
        "outcome": outcome,
        "at": datetime.now(timezone.utc).isoformat(),
        "ttl_hours": research_ttl_hours(_as_float(md.get("days_to_resolution"))),
        **values,
    }
    _write_filter_state(
        vault, market_id, prior, research_state=state, refresh_reason=None
    )


def _skip_research(
    vault: ObsidianManager,
    row: dict[str, Any],
    stage: str,
    reason: str,
    **values: Any,
) -> None:
    market_id = row["market_id"]
    _ledger_safe(
        vault,
        market_id,
        "research_skip",
        reason=reason,
        research_stage=stage,
        **values,
        **_ledger_market_fields(row.get("market_data"), role="deep_researcher"),
    )
    _record_attempt(vault, row, stage, reason, **values)
    log.info("[PHASE 3] %s research_skip %s for %s", stage, reason, market_id)
