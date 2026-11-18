"""Phase 4 - Execution: deterministic sizing and the in-process trade skill (no agent)."""

from __future__ import annotations

import importlib.util
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from obsidian_utils import ObsidianManager
from config.trading_constants import BANKROLL_USD, LIQ_FRAC, effective_min_edge
from orchestrator import ledger, notify, polymarket_api, scraper, stats
from orchestrator.config import PAPER_TRADE_MODE
from orchestrator.phases.common import (
    _as_float,
    _carry_filter_signals,
    _ledger_market_fields,
    _ledger_safe,
    _trade_log_shows_edge_disqualification,
    log,
)
from orchestrator.scraper import market_data_hydration_error
from orchestrator.state import (
    flag_inactive,
    is_error_free_active,
    market_quarantine,
    vault_write_or_flag,
)

_ROOT = Path(__file__).resolve().parents[2]
# measure-only independent-evidence sample fields: copied from research to the ledger, never used for sizing
_INDEP_KEYS = ("indep_p", "indep_p_low", "indep_p_high", "indep_source", "indep_gap", "indep_error")
_ALLOCATION_SKILL_PATH = (
    _ROOT / "skills/calculate-trade-allocation/calculate_trade_allocation.py"
)
_allocation_fn: Callable[..., Any] | None = None
_TRADE_SKILL_PATH = (
    _ROOT / "skills/execute-polymarket-trade/execute_polymarket_trade.py"
)
_trade_fn: Callable[..., Any] | None = None


def _load_calculate_trade_allocation() -> Callable[..., Any]:
    global _allocation_fn
    if _allocation_fn is not None:
        return _allocation_fn
    spec = importlib.util.spec_from_file_location(
        "calculate_trade_allocation", _ALLOCATION_SKILL_PATH
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load skill from {_ALLOCATION_SKILL_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _allocation_fn = mod.calculate_trade_allocation
    return _allocation_fn


def _load_execute_polymarket_trade() -> Callable[..., Any]:
    global _trade_fn
    if _trade_fn is not None:
        return _trade_fn
    spec = importlib.util.spec_from_file_location(
        "execute_polymarket_trade", _TRADE_SKILL_PATH
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load skill from {_TRADE_SKILL_PATH}")
    mod = importlib.util.module_from_spec(spec)
    # pydantic resolves the skill's postponed annotations through sys.modules.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    _trade_fn = mod.execute_polymarket_trade
    return _trade_fn


def _open_allocated_usd(vault: ObsidianManager, event_id: str | None = None) -> float:
    """Sum ``allocation_usd`` over open (unresolved) trade logs, optionally for one event."""
    total = 0.0
    for path in vault.iter_open_trades():
        record = vault.read_trade_log_dict(path.stem)
        if not record:
            continue
        if event_id is not None and record.get("event_id") != event_id:
            continue
        try:
            total += max(0.0, float(record.get("allocation_usd") or 0.0))
        except (TypeError, ValueError):
            continue
    return total


def compute_allocation(
    vault: ObsidianManager,
    p: float,
    q: float,
    market_data: dict[str, Any],
    p_low: float | None = None,
    p_high: float | None = None,
    quotes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run ``calculate_trade_allocation`` in-process against bankroll + realised P&L.

    ``quotes`` (``{"YES": quote, "NO": quote}`` from ``polymarket_api.executable_quote``) supplies
    the real asks (``q_yes_ask`` / ``q_no_ask``) and caps the order at the chosen side's
    ``depth_usd_within_2c``; without it the mid ``q`` is the YES ask and NO is ``1 - q``.
    """
    try:
        days = int(float(market_data.get("days_to_resolution")))
    except (TypeError, ValueError):
        days = None  # the skill reports an input error
    event_id = market_data.get("event_id")
    bankroll = BANKROLL_USD + stats.realised_pnl(list(ledger.iter_rows(vault)))
    liquidity = market_data.get("liquidity")

    def run(L: Any) -> dict[str, Any]:
        result = _load_calculate_trade_allocation()(
            p=p,
            q_yes_ask=quotes["YES"]["ask"] if quotes else q,
            q_no_ask=quotes["NO"]["ask"] if quotes else None,
            D=days,
            L=L,
            bankroll=bankroll,
            open_exposure_total=_open_allocated_usd(vault),
            open_exposure_event=_open_allocated_usd(vault, event_id) if event_id else 0.0,
            p_low=p_low,
            p_high=p_high,
        )
        return result.model_dump() if hasattr(result, "model_dump") else dict(result)

    out = run(liquidity)
    side = out.get("side")
    if quotes and side in quotes and not out.get("error"):
        depth = _as_float(quotes[side].get("depth_usd_within_2c"))
        # The skill caps an order at LIQ_FRAC * L; shrinking L to depth / LIQ_FRAC turns that
        # cap into "no more than the ask depth within 2c" without touching the skill.
        if depth is not None and out["allocation_usd"] > depth and isinstance(liquidity, (int, float)):
            out = run(min(float(liquidity), depth / LIQ_FRAC))
    return out


def _phase4_quotes(market_id: str) -> dict[str, dict[str, Any]] | None:
    """Real YES and NO asks + depth from the CLOB, or ``None`` (infra error: skip this tick, no flag)."""
    try:
        yes = polymarket_api.executable_quote(market_id, "YES")
        no = polymarket_api.executable_quote(market_id, "NO")
    except polymarket_api.PolymarketAPIError as exc:
        log.warning("[PHASE 4] quote fetch failed for %s: %s", market_id, exc)
        return None
    return {"YES": yes, "NO": no} if yes and no else None


def _phase4_queue(
    vault: ObsidianManager,
    researched_markets: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], bool]]:
    """This tick's researched markets plus a vault scan; ``(row, from_research)`` deduped by id."""
    queue: list[tuple[dict[str, Any], bool]] = []
    seen: set[str] = set()
    for row in researched_markets:
        if row["market_id"] not in seen:
            seen.add(row["market_id"])
            queue.append((row, True))

    for path in vault.iter_dir_files("active", ".md"):
        market_id = path.stem
        if market_id in seen:
            continue
        if (
            not is_error_free_active(vault, market_id)
            or vault.market_file(market_id, "trades") is not None
            or vault.has_execution_intent(market_id)
            or vault.is_market_inactive(market_id, dir_key="filters")
        ):
            continue
        fm = vault.read_market_record(market_id, "active") or {}
        try:
            p_value = float(fm["estimated_p"])
        except (KeyError, TypeError, ValueError):
            continue
        fresh = scraper.fetch_market_row(market_id)
        if fresh is None:
            log.warning("[PHASE 4] skip %s: could not hydrate market row", market_id)
            continue
        seen.add(market_id)
        queue.append(
            (
                {
                    "market_id": market_id,
                    "p_value": p_value,
                    "p_low": _as_float(fm.get("p_low")),
                    "p_high": _as_float(fm.get("p_high")),
                    "evidence_source": fm.get("evidence_source"),
                    "evidence_fallback": fm.get("evidence_fallback"),
                    **{k: fm.get(k) for k in _INDEP_KEYS if fm.get(k) is not None},
                    "market_data": _carry_filter_signals(
                        fresh.market_data, vault.read_filter_log(market_id)
                    ),
                },
                False,
            )
        )
    return queue


def _may_execute(vault: ObsidianManager, market_id: str, from_research: bool) -> bool:
    """False when an intent or a (non-refreshable) trade log already exists for the market."""
    if vault.has_execution_intent(market_id):
        log.error(
            "[PHASE 4] refusing %s: execution intent exists from an earlier "
            "attempt (outcome unknown); resolve manually and delete the intent",
            market_id,
        )
        return False
    existing = vault.read_trade_log_dict(market_id)
    if vault.market_file(market_id, "trades") is not None:
        # Only an edge-disqualified trade from a fresh research refresh may re-execute.
        if not (
            from_research
            and existing is not None
            and _trade_log_shows_edge_disqualification(existing)
        ):
            log.info("[PHASE 4] skip %s: trade log already exists", market_id)
            return False
    return True


def _trade_record(
    vault: ObsidianManager,
    market_id: str,
    market_data: dict[str, Any],
    q: float,
    row: dict[str, Any],
    allocation: dict[str, Any],
    size_usd: float,
    below: bool,
    bought: dict[str, Any],
) -> dict[str, Any]:
    return {
        "market_id": market_id,
        "allocation_usd": size_usd,
        "score": allocation.get("edge"),
        "below_edge_threshold": below,
        "executed": False,
        "transaction_hash": None,
        "error": None,
        "p": row["p_value"],
        "p_low": row.get("p_low"),
        "p_high": row.get("p_high"),
        "q_entry": q,
        "side": allocation.get("side"),
        "price": allocation.get("price"),
        "ask": bought.get("ask"),
        "depth_usd": bought.get("depth_usd_within_2c"),
        "shares": allocation.get("shares"),
        "directive_version": ledger.current_directive_version(vault),
        "event_id": market_data.get("event_id"),
    }


def _place_trade(
    vault: ObsidianManager,
    market_id: str,
    allocation: dict[str, Any],
    size_usd: float,
    trade: dict[str, Any],
) -> bool:
    """Write the intent, call the trade skill, update ``trade``. ``False`` when the call failed."""
    vault.write_execution_intent(
        market_id,
        {
            "market_id": market_id,
            "started_at": time.time(),
            "paper_trade_mode": bool(PAPER_TRADE_MODE),
            "allocation": allocation,
        },
    )
    result = _load_execute_polymarket_trade()(
        market_id=market_id,
        outcome=allocation["side"],
        amount=size_usd,
        price=allocation.get("price"),
    )
    res = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    if not res.get("success"):
        reason = f"execute_polymarket_trade failed: {res.get('error')}"
        log.error(
            "[PHASE 4] trade failed for %s (%s); intent left in place, "
            "market will not be re-executed automatically",
            market_id,
            reason,
        )
        flag_inactive(vault, market_id, "phase4", reason, {**trade, "result": res})
        return False
    if res.get("paper"):
        log.info("[PAPER_TRADE] %s %s $%.2f", market_id, allocation["side"], size_usd)
    trade["executed"] = bool(res.get("success")) and not res.get("paper")
    trade["transaction_hash"] = res.get("transaction_hash")
    return True


def _persist_trade(
    vault: ObsidianManager,
    row: dict[str, Any],
    q: float,
    trade: dict[str, Any],
    allocation: dict[str, Any],
    size_usd: float,
    below: bool,
    bought: dict[str, Any],
) -> None:
    """Write the trade log, clear the intent and ledger ``traded`` / ``below_edge``."""
    market_id = row["market_id"]
    market_data = row.get("market_data") or {}
    if vault_write_or_flag(
        vault=vault,
        market_id=market_id,
        write_fn=lambda: vault.write_trade_log(market_id, trade),
        payload=trade,
        artifact_label="trade log",
        phase="phase4",
    ):
        vault.clear_execution_intent(market_id)
        _ledger_safe(
            vault,
            market_id,
            "below_edge" if below else "traded",
            reason="below_edge_threshold" if below else None,
            q=q,
            p=row["p_value"],
            p_low=row.get("p_low"),
            p_high=row.get("p_high"),
            side=allocation.get("side"),
            price=allocation.get("price"),
            ask=bought.get("ask"),
            depth_usd=bought.get("depth_usd_within_2c"),
            shares=allocation.get("shares"),
            size_usd=size_usd,
            evidence_source=row.get("evidence_source"),
            evidence_fallback=row.get("evidence_fallback"),
            **{k: row[k] for k in _INDEP_KEYS if row.get(k) is not None},
            lam=allocation.get("lam"),
            min_edge=allocation.get("min_edge"),
            **_ledger_market_fields(market_data),
        )


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


def _execute_market(vault: ObsidianManager, row: dict[str, Any], from_research: bool) -> None:
    """Guards -> quotes -> sizing -> (trade skill) -> trade log + ledger for one market."""
    market_id = row["market_id"]
    market_data = row.get("market_data") or {}
    if not _may_execute(vault, market_id, from_research):
        return

    q = scraper.select_q(market_data)
    if q is None:
        log.warning("[PHASE 4] skip %s this tick: no usable q", market_id)
        return
    hydration_err = market_data_hydration_error(market_data)
    if hydration_err:
        log.warning("[PHASE 4] skip %s this tick: %s", market_id, hydration_err)
        return

    quotes = _phase4_quotes(market_id)
    if quotes is None:
        log.warning("[PHASE 4] skip %s this tick: executable quote unavailable", market_id)
        return

    p_low, p_high = row.get("p_low"), row.get("p_high")
    allocation = compute_allocation(
        vault, row["p_value"], q, market_data, p_low, p_high, quotes=quotes
    )
    if allocation.get("error"):
        log.warning(
            "[PHASE 4] skip %s this tick: allocation error: %s",
            market_id,
            allocation["error"],
        )
        return

    size_usd = float(allocation["allocation_usd"])
    below = bool(allocation["below_edge_threshold"]) or not size_usd > 0
    bought = quotes.get(allocation.get("side") or "") or {}
    trade = _trade_record(vault, market_id, market_data, q, row, allocation, size_usd, below, bought)

    if not below and notify.approval_enabled():
        _park_offer(vault, row, q, trade, allocation, size_usd, bought)
        return
    if not below and not _place_trade(vault, market_id, allocation, size_usd, trade):
        return
    _persist_trade(vault, row, q, trade, allocation, size_usd, below, bought)
    if not below:
        notify.emit_offer(vault, trade, market_data, mode="informational")


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
    """Re-quote + re-size on a yes vote. ``done`` | ``retry`` (infra problem: keep vote for next poll)."""
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
        return "done"
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


def process_inbox(vault: ObsidianManager, now: datetime | None = None) -> dict[str, int]:
    """Apply votes from ``06_Concierge/inbox/vote-<offer_id>.json`` and expire stale pending offers.

    yes before expiry: re-quote + re-size, execute only if the edge still clears
    ``effective_min_edge()`` (else ``offer_stale``). no: ``offer_declined``. Past expiry:
    ``offer_expired``. Processed votes/offers move to ``_done/``. Never raises per file.
    """
    now = now or datetime.now(tz=timezone.utc)
    summary = {"approved": 0, "stale": 0, "declined": 0, "expired": 0, "ignored": 0, "retry": 0}
    inbox, outbox = notify.inbox_dir(vault), notify.outbox_dir(vault)

    def expired(offer: dict[str, Any]) -> bool:
        exp = notify.parse_ts(offer.get("expires_at"))
        return exp is not None and now > exp

    for vote_path in sorted(inbox.glob("vote-*.json")):
        if vote_path.stem.startswith("vote-" + notify.DIRECTIVE_ID_PREFIX):
            continue  # Overseer directive vote: owned by p6_overseer.process_directive_votes
        try:
            vote = notify.read_json(vote_path) or {}
            offer_id = str(vote.get("offer_id") or "")
            choice = str(vote.get("vote") or "").strip().lower()
            offer_file = outbox / f"offer-{offer_id}.json" if offer_id else None
            offer = notify.read_json(offer_file) if offer_file and offer_file.exists() else None
            if offer is None or offer.get("mode") != "approval" or choice not in ("yes", "no"):
                log.warning("[INBOX] ignoring vote %s (unknown offer or bad vote)", vote_path.name)
                summary["ignored"] += 1
                notify.move_to_done(vote_path)
                continue
            market_id = offer["market_id"]
            if not any(
                r.get("stage") == "user_vote" and r.get("offer_id") == offer_id for r in ledger.iter_rows(vault)
            ):  # a retried vote is ledgered once
                _ledger_safe(vault, market_id, "user_vote", offer_id=offer_id, vote=choice)
            with market_quarantine(vault, market_id, "phase4"):
                if expired(offer):
                    _close_offer(vault, offer, "offer_expired", "expired_before_vote")
                    summary["expired"] += 1
                elif choice == "no":
                    _close_offer(vault, offer, "offer_declined", "user_vote_no")
                    summary["declined"] += 1
                else:
                    outcome = _approve_offer(vault, offer)
                    if outcome == "retry":
                        summary["retry"] += 1
                        continue  # keep vote + offer; next poll retries (or the offer expires)
                    stale = any(
                        r.get("offer_id") == offer_id and r.get("stage") == "offer_stale"
                        for r in ledger.iter_rows(vault)
                    )
                    summary["stale" if stale else "approved"] += 1
            notify.move_to_done(offer_file)
            notify.move_to_done(vote_path)
        except Exception:  # noqa: BLE001 - one bad vote must not block the rest
            log.exception("[INBOX] failed processing %s", vote_path.name)

    for offer_file in sorted(outbox.glob("offer-*.json")):  # unvoted offers past their expiry
        try:
            offer = notify.read_json(offer_file)
            if offer and offer.get("mode") == "approval" and expired(offer):
                with market_quarantine(vault, offer["market_id"], "phase4"):
                    _close_offer(vault, offer, "offer_expired", "expired_no_vote")
                summary["expired"] += 1
                notify.move_to_done(offer_file)
        except Exception:  # noqa: BLE001
            log.exception("[INBOX] failed expiring %s", offer_file.name)
    return summary


def phase4_execution(
    vault: ObsidianManager,
    researched_markets: list[dict[str, Any]],
) -> None:
    """Deterministic execution: Hub sizing -> ``execute_polymarket_trade`` -> trade log + ledger.

    No agent is involved. ``compute_allocation`` (shrink toward q, quarter-Kelly, YES/NO
    side) sizes the trade; a zero allocation is ledgered ``below_edge``. Otherwise the
    trade skill is called in-process (``paper: true`` means simulated). An intent record
    guards against double execution across ticks and is left in place on failure.
    """
    log.info("[PHASE 4] Execution")

    for row, from_research in _phase4_queue(vault, researched_markets):
        with market_quarantine(vault, row["market_id"], "phase4"):
            _execute_market(vault, row, from_research)
