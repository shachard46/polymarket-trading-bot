"""Helpers shared across the pipeline phases (ledger, trade-log predicates, payloads)."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Literal

from obsidian_utils import FilterLogPayload, ObsidianManager
from config.trading_constants import BELOW_EDGE_KEY
from orchestrator import ledger
from orchestrator.state import vault_write_or_flag

# One logger name for the whole package (the module used to be ``orchestrator.phases``).
log = logging.getLogger("orchestrator.phases")

Phase3Kind = Literal["initial", "refresh"]


@dataclass(frozen=True)
class Phase3Candidate:
    """One market eligible for the qualitative pipeline."""

    market_id: str
    kind: Phase3Kind
    filter_record: dict[str, Any]


# ---------------------------------------------------------------------------


_ledger_lock = threading.Lock()  # FULL-stage markets append to one ledger file concurrently


def _trade_log_has_nonempty_error(data: dict[str, Any]) -> bool:
    err = data.get("error")
    if err is None:
        return False
    return bool(str(err).strip())


def _open_trade_shows_bet_not_edge_dq(data: dict[str, Any]) -> bool:
    """True when an open trade log reflects a non-zero allocation path (bet placed)."""
    if _trade_log_has_nonempty_error(data):
        return False
    bet = data.get(BELOW_EDGE_KEY)
    if bet is False:
        return True
    if bet is True:
        return False
    try:
        return float(data.get("allocation_usd") or 0.0) > 0.0
    except (TypeError, ValueError):
        return False


def _trade_log_shows_edge_disqualification(data: dict[str, Any]) -> bool:
    """True when the last run hit the edge gate (no allocation: best edge below ``MIN_EDGE``)."""
    if _trade_log_has_nonempty_error(data):
        return False
    bet = data.get(BELOW_EDGE_KEY)
    if bet is True:
        return True
    if bet is False:
        return False
    try:
        return float(data.get("allocation_usd") or 0.0) == 0.0
    except (TypeError, ValueError):
        return False


def _market_spread(md: dict[str, Any]) -> float | None:
    bid, ask = md.get("yes_bid"), md.get("yes_ask")
    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)):
        return round(float(ask) - float(bid), 6)
    return None


def _ledger_market_fields(market_data: dict[str, Any] | None) -> dict[str, Any]:
    md = market_data or {}
    return {
        "category": md.get("category"),
        "end_date": md.get("end_date"),
        "event_id": md.get("event_id"),
        "volume_24h": md.get("volume_24h"),
        "day_price_change": md.get("day_price_change"),
        "holder_top1": md.get("holder_top1"),
        "holder_top3": md.get("holder_top3"),
        "bid_ask_spread": _market_spread(md),
        "manipulation_risk": md.get("manipulation_risk") or "unknown",
    }


_MANIP_LEVELS = frozenset({"low", "medium", "high"})


def _filter_manipulation_risk(filter_record: dict[str, Any] | None) -> str:
    """Cached triage verdict's ``manipulation_risk`` from a filter record; ``unknown`` if absent."""
    triage = (filter_record or {}).get("triage")
    risk = triage.get("manipulation_risk") if isinstance(triage, dict) else None
    return risk if risk in _MANIP_LEVELS else "unknown"


def _carry_manipulation_risk(
    market_data: dict[str, Any], filter_record: dict[str, Any] | None
) -> dict[str, Any]:
    """Copy the filter record's triage verdict into ``market_data`` (in place) for ledger rows."""
    if market_data.get("manipulation_risk") not in _MANIP_LEVELS:
        market_data["manipulation_risk"] = _filter_manipulation_risk(filter_record)
    return market_data


def _ledger_safe(
    vault: ObsidianManager, market_id: str, stage: str, **fields: Any
) -> dict[str, Any] | None:
    """Ledger write that can never break the pipeline."""
    try:
        with _ledger_lock:
            return ledger.record(vault, market_id, stage, **fields)
    except Exception:  # noqa: BLE001
        log.exception("ledger write failed for %s (%s)", market_id, stage)
        return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


_FILTER_FIELDS = tuple(FilterLogPayload.model_fields)


def _write_filter_state(
    vault: ObsidianManager,
    market_id: str,
    prior: dict[str, Any] | None,
    **updates: Any,
) -> bool:
    """Merge ``updates`` over the prior filter record and persist (keeps backoff/triage state)."""
    base = {k: v for k, v in (prior or {}).items() if k in _FILTER_FIELDS}
    payload = {
        "market_id": market_id,
        "passed": False,
        "trigger": None,
        "confidence_multiplier": 0.0,
        "details": "",
        "error": None,
        **base,
        **updates,
    }
    return vault_write_or_flag(
        vault=vault,
        market_id=market_id,
        write_fn=lambda: vault.write_filter_log(market_id, payload),
        payload=payload,
        artifact_label="filter log",
        phase="phase2",
    )


def _text_fields(row: dict[str, Any], today: str) -> dict[str, Any]:
    """Market text shared by every price-blind payload (never any price field)."""
    md = row.get("market_data") or {}
    return {
        "market_id": row["market_id"],
        "title": row.get("market_title", ""),
        "description": row.get("market_description", ""),
        "resolution_source": md.get("resolution_source"),
        "end_date": md.get("end_date"),
        "category": md.get("category"),
        "today": today,
    }


def _dr_payload(
    row: dict[str, Any],
    directives: str,
    bundle: list[dict[str, Any]],
    *,
    mode: Literal["quick", "full"],
    today: str,
    system_override: str | None = None,
    session_tag: str | None = None,
) -> dict[str, Any]:
    return {
        **_text_fields(row, today),
        "mode": mode,
        "directives": directives,
        "research_bundle": bundle,
        "system_override": system_override,
        "format_validation_error": None,
        "session_tag": session_tag,
    }
