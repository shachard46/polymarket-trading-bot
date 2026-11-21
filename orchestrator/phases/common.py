"""Helpers shared across the pipeline phases (ledger, trade-log predicates, payloads)."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Literal

from obsidian_utils import FilterLogPayload, ObsidianManager
from config.trading_constants import BELOW_EDGE_KEY
from orchestrator import evidence, ledger

from orchestrator.config import (
    RUNNER_MODE_LIVE,
    runner_mode,
)
from orchestrator.state import vault_write_or_flag
from orchestrator.util import to_float

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


def _ledger_model_fields(
    role: str | None = None, market_id: str | None = None, judge_fallback: bool | None = None
) -> dict[str, Any]:
    """Runner backend plus, when the caller names the agent ``role``, the model that produced the row.

    Live mode adds ``provider`` and ``model`` (from ``routing.spec_for(role)``). Stub modes add neither.
    """
    mode = runner_mode()
    out: dict[str, Any] = {"runner_backend": mode}
    if not role:
        return out
    if role == "deep_researcher" and market_id:
        # Judge model A/B: the arm and the provider/model that actually answered (override or primary fallback).
        from orchestrator import judge_model  # noqa: PLC0415

        out.update(judge_model.ledger_fields(market_id, judge_fallback))
        if mode != RUNNER_MODE_LIVE:
            out.pop("provider", None)
            out.pop("model", None)
        return out
    if mode == RUNNER_MODE_LIVE:
        from orchestrator.llm import routing  # noqa: PLC0415 - avoid import cycle at module load

        try:
            spec = routing.spec_for(role)
            out["provider"], out["model"] = spec.provider, spec.model
        except Exception:  # noqa: BLE001 - startup check should have caught this; never break a ledger write
            out["provider"], out["model"] = None, None
    return out


def _ledger_market_fields(
    market_data: dict[str, Any] | None, role: str | None = None, market_id: str | None = None,
    judge_fallback: bool | None = None,
) -> dict[str, Any]:
    """Market-derived ledger fields plus the runner backend (and model/provider when ``role`` is given)."""
    md = market_data or {}
    return {
        **_ledger_model_fields(role, market_id, judge_fallback),
        "category": md.get("category"),
        "end_date": md.get("end_date"),
        "event_id": md.get("event_id"),
        "volume_24h": md.get("volume_24h"),
        "day_price_change": md.get("day_price_change"),
        "holder_top1": md.get("holder_top1"),
        "holder_top3": md.get("holder_top3"),
        "holder_basis": md.get("holder_basis"),
        "bid_ask_spread": _market_spread(md),
        "manipulation_risk": md.get("manipulation_risk") or "unknown",
        "integ_score": md.get("integ_score"),
        "integ_flags": md.get("integ_flags"),
    }


_MANIP_LEVELS = frozenset({"low", "medium", "high"})


def _filter_manipulation_risk(filter_record: dict[str, Any] | None) -> str:
    """Cached triage verdict's ``manipulation_risk`` from a filter record; ``unknown`` if absent."""
    triage = (filter_record or {}).get("triage")
    risk = triage.get("manipulation_risk") if isinstance(triage, dict) else None
    return risk if risk in _MANIP_LEVELS else "unknown"


def _filter_integrity_fields(filter_record: dict[str, Any] | None) -> dict[str, Any]:
    """``integ_score``/``integ_flags`` from a filter record's integrity dict (None when absent)."""
    integ = (filter_record or {}).get("integrity")
    integ = integ if isinstance(integ, dict) else {}
    return {"integ_score": integ.get("score"), "integ_flags": integ.get("flags")}


def _carry_filter_signals(
    market_data: dict[str, Any], filter_record: dict[str, Any] | None
) -> dict[str, Any]:
    """Copy the filter record's triage verdict and integrity score/flags into ``market_data`` (in place)."""
    if market_data.get("manipulation_risk") not in _MANIP_LEVELS:
        market_data["manipulation_risk"] = _filter_manipulation_risk(filter_record)
    integ = (filter_record or {}).get("integrity")
    if isinstance(integ, dict):
        for key in ("score", "flags"):
            if market_data.get(f"integ_{key}") is None and integ.get(key) is not None:
                market_data[f"integ_{key}"] = integ[key]
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


_as_float = to_float


_FILTER_FIELDS = tuple(FilterLogPayload.model_fields)


def _write_filter_state(
    vault: ObsidianManager,
    market_id: str,
    prior: dict[str, Any] | None,
    **updates: Any,
) -> bool:
    """Merge ``updates`` over the prior filter record and persist (keeps backoff/triage state)."""
    base = {k: v for k, v in (prior or {}).items() if k in _FILTER_FIELDS}
    first_hash = base.get("description_first_hash") or updates.get("description_first_hash")
    payload = {
        "market_id": market_id,
        "passed": False,
        "trigger": None,
        "confidence_multiplier": 0.0,
        "details": "",
        "error": None,
        **base,
        **updates,
        "description_first_hash": first_hash,  # set once, never overwritten
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
    mode: Literal["quick", "full", "judge"],
    today: str,
    system_override: str | None = None,
    session_tag: str | None = None,
    ask: Literal["yes", "no"] | None = None,
    crux_table: list[dict[str, Any]] | None = None,
    known_unknowns: list[str] | None = None,
    compound: dict[str, Any] | None = None,
    playbook: str | None = None,
    only_ids: set[str] | None = None,
) -> dict[str, Any]:
    optional = {k: v for k, v in (("compound", compound), ("playbook", playbook)) if v}
    return {
        **_text_fields(row, today),
        "mode": mode,
        "directives": directives,
        # ids + ages (+ source quality in judge mode) at render time only
        "research_bundle": evidence.label_bundle(bundle, today, quality=mode == "judge", only_ids=only_ids),
        "system_override": system_override,
        "format_validation_error": None,
        "session_tag": session_tag,
        "ask": ask,
        "crux_table": crux_table,
        "known_unknowns": known_unknowns,
        **optional,  # only when set: a compound spec / category playbook (optional payload keys)
    }
