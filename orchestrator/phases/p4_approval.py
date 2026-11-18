"""Phase 4 approval mode: park offers, apply yes/no votes from the concierge inbox, expire stale offers."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.trading_constants import effective_min_edge
from obsidian_utils import ObsidianManager
from orchestrator import ledger, notify, scraper
from orchestrator.phases.common import (
    _as_float,
    _carry_filter_signals,
    _ledger_market_fields,
    _ledger_safe,
    log,
)
from orchestrator.phases.p4_execution import (
    _INDEP_KEYS,
    _persist_trade,
    _phase4_quotes,
    _place_trade,
    _trade_record,
    compute_allocation,
)
from orchestrator.scraper import market_data_hydration_error
from orchestrator.state import market_quarantine


def _park_offer(
    vault: ObsidianManager,
    row: dict[str, Any],
    q: float,
    trade: dict[str, Any],
    allocation: dict[str, Any],
    size_usd: float,
    bought: dict[str, Any],
) -> None:
    """Approval mode: write a pending offer (outbox + intent + ledger ``offer_pending``) instead of trading."""
    market_id = row["market_id"]
    market_data = row.get("market_data") or {}
    resume = {
        "row": {
            k: row.get(k)
            for k in ("p_value", "p_low", "p_high", "evidence_source", "evidence_fallback", *_INDEP_KEYS)
            if row.get(k) is not None
        },
        "market_fields": _ledger_market_fields(market_data),
        "lam": allocation.get("lam"),
        "min_edge": allocation.get("min_edge"),
    }
    offer = notify.emit_offer(vault, trade, market_data, mode="approval", resume=resume)
    if offer is None:
        log.warning("[PHASE 4] %s: offer could not be written; not parked, retried next tick", market_id)
        return
    vault.write_execution_intent(
        market_id,
        {"market_id": market_id, "started_at": time.time(), "pending_offer": offer["offer_id"]},
    )
    _ledger_safe(vault, market_id, "offer_pending", **_offer_row_fields(offer))
    log.info("[PHASE 4] offer pending for %s (%s)", market_id, offer["offer_id"])


def _offer_row_fields(offer: dict[str, Any], **override: Any) -> dict[str, Any]:
    """Ledger fields of an offer (stated bet + research fields), for offer_* rows."""
    resume = offer.get("resume") or {}
    row = resume.get("row") or {}
    fields: dict[str, Any] = {
        "offer_id": offer["offer_id"],
        "q": offer.get("q_entry"),
        "p": offer.get("p"),
        "p_low": offer.get("p_low"),
        "p_high": offer.get("p_high"),
        "side": offer.get("side"),
        "price": offer.get("price"),
        "ask": offer.get("ask"),
        "depth_usd": offer.get("depth_usd"),
        "shares": offer.get("shares"),
        "size_usd": offer.get("allocation_usd"),
        "edge": offer.get("edge"),
        "expires_at": offer.get("expires_at"),
        "evidence_source": row.get("evidence_source"),
        "evidence_fallback": row.get("evidence_fallback"),
        **{k: row[k] for k in _INDEP_KEYS if row.get(k) is not None},
        "lam": resume.get("lam"),
        "min_edge": resume.get("min_edge"),
        **(resume.get("market_fields") or {}),
    }
    fields.update(override)
    return fields


def _close_offer(vault: ObsidianManager, offer: dict[str, Any], stage: str, reason: str) -> None:
    """Final non-execution of an offer: ledger row, blocking trade log (allocation 0), intent cleared."""
    market_id = offer["market_id"]
    _ledger_safe(vault, market_id, stage, reason=reason, **_offer_row_fields(offer))
    try:
        vault.write_trade_log(
            market_id,
            {
                "market_id": market_id,
                "allocation_usd": 0.0,
                "score": offer.get("edge"),
                # stale = edge gone: a fresh research refresh may re-offer. declined/expired: do not nag.
                "below_edge_threshold": stage == "offer_stale",
                "executed": False,
                "p": offer.get("p"),
                "p_low": offer.get("p_low"),
                "p_high": offer.get("p_high"),
                "q_entry": offer.get("q_entry"),
                "side": offer.get("side"),
                "directive_version": ledger.current_directive_version(vault),
                "event_id": (offer.get("resume") or {}).get("market_fields", {}).get("event_id"),
            },
        )
    except Exception:  # noqa: BLE001
        log.exception("[PHASE 4] could not write closing trade log for %s", market_id)
    vault.clear_execution_intent(market_id)


def _approve_offer(vault: ObsidianManager, offer: dict[str, Any]) -> str:
    """Re-quote + re-size on a yes vote. ``stale`` (edge gone, offer closed) | ``done`` | ``retry`` (infra problem: keep vote for next poll)."""
    market_id = offer["market_id"]
    resume = offer.get("resume") or {}
    stored = resume.get("row") or {}
    fresh = scraper.fetch_market_row(market_id)
    if fresh is None:
        log.warning("[INBOX] %s: could not hydrate market row; retry next poll", market_id)
        return "retry"
    market_data = _carry_filter_signals(fresh.market_data, vault.read_filter_log(market_id))
    q = scraper.select_q(market_data)
    quotes = _phase4_quotes(market_id)
    if q is None or quotes is None or market_data_hydration_error(market_data):
        log.warning("[INBOX] %s: fresh q/quote unavailable; retry next poll", market_id)
        return "retry"
    row = {
        "market_id": market_id,
        "p_value": stored.get("p_value", offer.get("p")),
        "p_low": stored.get("p_low"),
        "p_high": stored.get("p_high"),
        **{k: v for k, v in stored.items() if k in ("evidence_source", "evidence_fallback", *_INDEP_KEYS)},
        "market_data": market_data,
    }
    allocation = compute_allocation(
        vault, row["p_value"], q, market_data, row["p_low"], row["p_high"], quotes=quotes
    )
    size_usd = float(allocation.get("allocation_usd") or 0.0)
    edge = _as_float(allocation.get("edge"))
    ok = (
        not allocation.get("error")
        and not allocation.get("below_edge_threshold")
        and size_usd > 0
        and edge is not None
        and edge >= effective_min_edge()
    )
    if not ok:
        log.info("[INBOX] %s: edge gone on re-quote (edge=%s); offer_stale", market_id, edge)
        _close_offer(vault, {**offer, "fresh_edge": edge}, "offer_stale", "edge_below_min_on_requote")
        return "stale"
    bought = quotes.get(allocation.get("side") or "") or {}
    _ledger_safe(
        vault, market_id, "offer_approved",
        **_offer_row_fields(
            offer, q=q, side=allocation.get("side"), price=allocation.get("price"), ask=bought.get("ask"),
            depth_usd=bought.get("depth_usd_within_2c"), shares=allocation.get("shares"),
            size_usd=size_usd, edge=edge,
        ),
    )
    trade = _trade_record(vault, market_id, market_data, q, row, allocation, size_usd, False, bought)
    if _place_trade(vault, market_id, allocation, size_usd, trade):
        _persist_trade(vault, row, q, trade, allocation, size_usd, False, bought)
    return "done"


def _offer_expired(offer: dict[str, Any], now: datetime) -> bool:
    exp = notify.parse_ts(offer.get("expires_at"))
    return exp is not None and now > exp


def _process_vote(
    vault: ObsidianManager,
    vote_path: Path,
    outbox: Path,
    now: datetime,
    voted: set[str],
    summary: dict[str, int],
) -> None:
    """Apply one vote file; moves vote + offer to ``_done/`` unless the vote must be retried."""
    vote = notify.read_json(vote_path) or {}
    offer_id = str(vote.get("offer_id") or "")
    choice = str(vote.get("vote") or "").strip().lower()
    offer_file = outbox / f"offer-{offer_id}.json" if offer_id else None
    offer = notify.read_json(offer_file) if offer_file and offer_file.exists() else None
    if offer is None or offer.get("mode") != "approval" or choice not in ("yes", "no"):
        log.warning("[INBOX] ignoring vote %s (unknown offer or bad vote)", vote_path.name)
        summary["ignored"] += 1
        notify.move_to_done(vote_path)
        return
    market_id = offer["market_id"]
    if offer_id not in voted:  # a retried vote is ledgered once
        _ledger_safe(vault, market_id, "user_vote", offer_id=offer_id, vote=choice)
        voted.add(offer_id)
    with market_quarantine(vault, market_id, "phase4"):
        if _offer_expired(offer, now):
            _close_offer(vault, offer, "offer_expired", "expired_before_vote")
            summary["expired"] += 1
        elif choice == "no":
            _close_offer(vault, offer, "offer_declined", "user_vote_no")
            summary["declined"] += 1
        else:
            outcome = _approve_offer(vault, offer)
            if outcome == "retry":
                summary["retry"] += 1
                return  # keep vote + offer; next poll retries (or the offer expires)
            summary["stale" if outcome == "stale" else "approved"] += 1
    notify.move_to_done(offer_file)
    notify.move_to_done(vote_path)


def _expire_unvoted(vault: ObsidianManager, outbox: Path, now: datetime, summary: dict[str, int]) -> None:
    """Close unvoted approval offers past their expiry."""
    for offer_file in sorted(outbox.glob("offer-*.json")):
        try:
            offer = notify.read_json(offer_file)
            if offer and offer.get("mode") == "approval" and _offer_expired(offer, now):
                with market_quarantine(vault, offer["market_id"], "phase4"):
                    _close_offer(vault, offer, "offer_expired", "expired_no_vote")
                summary["expired"] += 1
                notify.move_to_done(offer_file)
        except Exception:  # noqa: BLE001
            log.exception("[INBOX] failed expiring %s", offer_file.name)


def process_inbox(vault: ObsidianManager, now: datetime | None = None) -> dict[str, int]:
    """Apply votes from ``06_Concierge/inbox/vote-<offer_id>.json`` and expire stale pending offers.

    yes before expiry: re-quote + re-size, execute only if the edge still clears
    ``effective_min_edge()`` (else ``offer_stale``). no: ``offer_declined``. Past expiry:
    ``offer_expired``. Processed votes/offers move to ``_done/``. Never raises per file.
    """
    now = now or datetime.now(tz=timezone.utc)
    summary = {"approved": 0, "stale": 0, "declined": 0, "expired": 0, "ignored": 0, "retry": 0}
    inbox, outbox = notify.inbox_dir(vault), notify.outbox_dir(vault)
    voted = {  # read the ledger once; offers already vote-ledgered
        r.get("offer_id") for r in ledger.iter_rows(vault) if r.get("stage") == "user_vote"
    }

    for vote_path in sorted(inbox.glob("vote-*.json")):
        if vote_path.stem.startswith("vote-" + notify.DIRECTIVE_ID_PREFIX):
            continue  # Overseer directive vote: owned by p6_overseer.process_directive_votes
        try:
            _process_vote(vault, vote_path, outbox, now, voted, summary)
        except Exception:  # noqa: BLE001 - one bad vote must not block the rest
            log.exception("[INBOX] failed processing %s", vote_path.name)

    _expire_unvoted(vault, outbox, now, summary)
    return summary
