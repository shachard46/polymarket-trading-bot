"""Phase 5 - Resolution & post-mortem."""

from __future__ import annotations

import os
from typing import Any

from obsidian_utils import ObsidianManager, VaultWriteError
from orchestrator import failure_tags, ledger, scraper, stats
from orchestrator.phases.agent_call import run_structured_agent
from orchestrator.phases.common import _as_float, _filter_manipulation_risk, _ledger_safe, log
from orchestrator.runner import AgentRunner, spawn_agent
from orchestrator.state import flag_inactive, is_inactive, market_quarantine

MAX_POST_MORTEM_ATTEMPTS = 3



def phase5_resolution_and_post_mortem(
    vault: ObsidianManager,
    runner: AgentRunner = spawn_agent,
) -> None:
    """Resolved markets → ``04_Post_Mortems/`` → analyst appends analysis."""
    log.info("[PHASE 5] Resolution & post-mortem")
    for trade_path in vault.iter_open_trades():
        market_id = trade_path.stem
        trade = vault.read_trade_log_dict(market_id)
        if trade is not None and is_inactive(trade):
            log.info("[PHASE 5] skip %s: trade log marked inactive", market_id)
            continue
        with market_quarantine(vault, market_id, "phase5"):
            _resolve_market(vault, runner, market_id)
    _resolve_ledger_markets(vault)


def _resolve_ledger_markets(vault: ObsidianManager) -> None:
    """Record resolutions for every unresolved ledger market (capped per tick)."""
    try:
        limit = int(os.environ.get("OPENCLAW_LEDGER_RESOLVE_LIMIT", "100"))
    except ValueError:
        limit = 100
    for market_id in ledger.unresolved_market_ids(vault)[: max(limit, 0)]:
        try:
            resolution = scraper.fetch_resolution(market_id)
            outcome_yes = ledger.outcome_yes_from_resolution(resolution)
            if outcome_yes is None:
                continue
            ledger.record_resolution(
                vault, market_id, outcome_yes, (resolution or {}).get("raw_outcome"), None,
                manipulation_risk=_filter_manipulation_risk(vault.read_filter_log(market_id)),
            )
        except Exception:  # noqa: BLE001 - one bad market must not stop the sweep
            log.exception("[PHASE 5] ledger resolution failed for %s", market_id)


def _post_mortem_failed(
    vault: ObsidianManager,
    market_id: str,
    reason: str,
    payload: dict[str, Any],
) -> None:
    """Count the failed attempt on the trade log; flag it inactive once the cap is hit."""
    trade = vault.read_trade_log_dict(market_id) or {}
    try:
        attempts = int(trade.get("post_mortem_attempts") or 0) + 1
    except (TypeError, ValueError):
        attempts = 1
    vault.patch_json(market_id, "trades", {"post_mortem_attempts": attempts})
    if attempts >= MAX_POST_MORTEM_ATTEMPTS:
        flag_inactive(vault, market_id, "phase5", reason, payload)
    else:
        log.warning(
            "[PHASE 5] post-mortem attempt %d/%d failed for %s (retry next tick): %s",
            attempts,
            MAX_POST_MORTEM_ATTEMPTS,
            market_id,
            reason,
        )


def _post_mortem_payload(
    vault: ObsidianManager, market_id: str, resolution: dict[str, Any]
) -> dict[str, Any]:
    """Structured Post-Mortem Analyst input: the decision, the outcome and the realised P&L.

    P&L reuses ``stats.bet_pnl`` (pre-tax per bet in annual_offset mode; the after-tax total is portfolio-level in stats.md) so the analyst, the ledger and
    ``stats.md`` agree. ``None`` fields mean "not available" (e.g. a below-edge market has no bet).
    """
    trade = vault.read_trade_log_dict(market_id) or {}
    research = vault.read_market_record(market_id, "post_mortem") or {}
    filt = vault.read_filter_log(market_id) or {}
    triage = filt.get("triage") if isinstance(filt.get("triage"), dict) else {}
    outcome_yes = ledger.outcome_yes_from_resolution(resolution)

    def first(*vals: Any) -> Any:
        return next((v for v in vals if v is not None), None)

    size = _as_float(trade.get("allocation_usd"))
    size = size if size and size > 0 else None
    price = _as_float(trade.get("price"))
    side = trade.get("side") if size else None
    pnl = None
    if outcome_yes is not None and size is not None and price is not None:
        pnl = stats.bet_pnl(
            {"price": price, "size_usd": size, "shares": trade.get("shares"), "side": side},
            outcome_yes,
        )
    checklist = research.get("resolution_checklist")
    return {
        "market_id": market_id,
        "original_research": vault.read_post_mortem(market_id),
        "execution_log": vault.read_trade_log(market_id),
        "resolution_data": resolution,
        "p": _as_float(first(trade.get("p"), research.get("estimated_p"))),
        "p_low": _as_float(first(trade.get("p_low"), research.get("p_low"))),
        "p_high": _as_float(first(trade.get("p_high"), research.get("p_high"))),
        "q_entry": _as_float(first(trade.get("q_entry"), research.get("q_at_research"))),
        "side": side,
        "price": price if size else None,
        "size_usd": size,
        "outcome_yes": outcome_yes,
        "realised_pnl_usd": pnl,
        "category": first(triage.get("category"), filt.get("category")),
        "manipulation_risk": _filter_manipulation_risk(filt),
        "gate_score": _as_float(first(filt.get("score"), filt.get("confidence_multiplier"))),
        "research_confidence": research.get("confidence"),
        "resolution_checklist": checklist if isinstance(checklist, list) else None,
    }


def _resolve_market(
    vault: ObsidianManager,
    runner: AgentRunner,
    market_id: str,
) -> None:
    """Resolve one market and append post-mortem analysis when possible.

    Idempotent: a post-mortem file left by an earlier failed attempt is reused.
    """
    resolution = scraper.fetch_resolution(market_id)
    if resolution is None:
        return

    if vault.post_mortem_path(market_id).exists():
        log.info("[PHASE 5] post-mortem file exists for %s; re-running analyst", market_id)
    else:
        try:
            vault.move_file(market_id, "active", "post_mortem")
        except FileNotFoundError:
            log.warning("No active research file to move for %s", market_id)
            return
        except FileExistsError:
            log.warning("Post-mortem file already exists for %s; re-running analyst", market_id)

    payload = _post_mortem_payload(vault, market_id, resolution)
    registry = failure_tags.load_registry(vault)
    payload.update(failure_tags.post_mortem_tag_context(registry))
    parsed, reason = run_structured_agent(runner, "post_mortem_analyst", payload)
    if reason:
        _post_mortem_failed(vault, market_id, reason, parsed or payload)
        return

    fclass, tag_slug, tag_def = failure_tags.normalize_failure_class(
        parsed.get("failure_class"), parsed.get("failure_tag_definition"), registry
    )
    to_append = {k: v for k, v in parsed.items() if k != "failure_tag_definition"}
    to_append["failure_class"] = fclass
    try:
        vault.append_post_mortem(market_id, to_append)
    except VaultWriteError as exc:
        _post_mortem_failed(
            vault,
            market_id,
            f"post-mortem append validation failed: {exc.cause}",
            parsed,
        )
        return

    if tag_slug:
        try:
            failure_tags.record_tag(vault, tag_slug, tag_def)
        except Exception:  # noqa: BLE001 - registry is advisory; never block the post-mortem
            log.exception("[PHASE 5] failure tag registry write failed for %s", market_id)
    _ledger_safe(
        vault, market_id, ledger.POST_MORTEM_STAGE,
        failure_class=fclass,
        outcome_yes=payload["outcome_yes"],
        pnl_usd=payload["realised_pnl_usd"],
        p=payload["p"], side=payload["side"], price=payload["price"],
        size_usd=payload["size_usd"], category=payload["category"],
        manipulation_risk=payload["manipulation_risk"],
    )
    try:
        vault.archive_trade(market_id)
    except FileNotFoundError:
        log.warning("Trade log already archived for %s", market_id)
