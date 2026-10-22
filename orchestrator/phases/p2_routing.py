"""Phase 2 - Gate + Triage: deterministic gate, rank, price-blind Triage, persist to ``01_Filters/``."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from obsidian_utils import ObsidianManager
from orchestrator import gate, ledger, scraper
from orchestrator.config import gate_budget, triage_budget
from orchestrator.directives import extract_filter_directives
from orchestrator.phases.agent_call import run_structured_or_flag
from orchestrator.phases.common import (
    _ledger_market_fields,
    _ledger_safe,
    _open_trade_shows_bet_not_edge_dq,
    _write_filter_state,
    log,
)
from orchestrator.phases.research_state import _error_free_active_fm, _refresh_reason
from orchestrator.runner import AgentRunner, spawn_agent
from orchestrator.scraper import MarketRow
from orchestrator.state import market_quarantine

_Eligible = tuple[float, MarketRow, dict[str, Any] | None]
_Researchable = tuple[float, MarketRow, dict[str, Any] | None, dict[str, Any]]


def _event_triage_cache(vault: ObsidianManager) -> dict[str, dict[str, Any]]:
    """``event_id -> cached triage verdict`` from every filter record on disk."""
    cache: dict[str, dict[str, Any]] = {}
    for path in vault.iter_dir_files("filters", ".md"):
        rec = vault.read_market_record(path.stem, "filters") or {}
        triage, event_id = rec.get("triage"), rec.get("event_id")
        if event_id and isinstance(triage, dict) and "researchable" in triage:
            cache.setdefault(str(event_id), triage)
    return cache


def _triage_payload(market: MarketRow, today: str) -> dict[str, Any]:
    md = market.market_data
    return {
        "market_id": market.market_id,
        "title": market.market_title,
        "description": market.market_description,
        "resolution_source": md.get("resolution_source"),
        "category": md.get("category"),
        "end_date": md.get("end_date"),
        "today": today,
    }


def _make_ledger_once(
    vault: ObsidianManager, last_decision: dict[str, dict[str, Any]]
) -> Callable[..., None]:
    """Ledger a decision stage once per state change (skips when it is already the latest)."""

    def ledger_once(market_id: str, stage: str, **fields: Any) -> None:
        if last_decision.get(market_id, {}).get("stage") == stage:
            return
        last_decision[market_id] = _ledger_safe(vault, market_id, stage, **fields) or {
            "stage": stage
        }

    return ledger_once


def _gate_market(
    vault: ObsidianManager,
    market: MarketRow,
    prior: dict[str, Any] | None,
    knobs: dict[str, Any],
    now: datetime,
    ledger_once: Callable[..., None],
) -> _Eligible | None:
    """Stage-1 verdict for one market: ``(score, market, prior)`` when eligible, else ``None``."""
    market_id = market.market_id
    trade = vault.read_trade_log_dict(market_id)
    if trade is not None and _open_trade_shows_bet_not_edge_dq(trade):
        log.info("[PHASE 2] skip %s: open trade log shows an active bet", market_id)
        return None
    md = market.market_data
    q = scraper.select_q(md)
    if vault.active_research_path(market_id).exists():
        # Researched markets are not re-gated; flag a refresh when the price moved.
        fm = _error_free_active_fm(vault, market_id)
        if (
            fm is not None
            and prior is not None
            and prior.get("refresh_reason") != "price_move"
            and gate.eligibility(md, knobs)[0]
            and _refresh_reason(
                vault, market_id, fm, prior.get("research_state"), q, now
            )
            == "price_move"
        ):
            _write_filter_state(vault, market_id, prior, refresh_reason="price_move")
        return None

    fields = _ledger_market_fields(md)

    combined = gate.arbitrage_combined(md)
    if combined is not None:
        ledger_once(
            market_id,
            "arb_opportunity",
            reason="arbitrage",
            q=q,
            combined_ask=combined,
            paper_only=True,
            **fields,
        )
        return None

    ok, reason = gate.eligibility(md, knobs)
    if not ok:
        fail = gate.register_failure(prior, q, now)
        _write_filter_state(
            vault,
            market_id,
            prior,
            passed=False,
            trigger=reason,
            confidence_multiplier=0.0,
            score=None,
            details=f"gate_reject: {reason}",
            event_id=fields["event_id"],
            **fail,
        )
        ledger_once(market_id, "gate_reject", reason=reason, q=q, **fields)
        return None

    return (gate.score(md, knobs), market, prior)


def _eligible_markets(
    vault: ObsidianManager,
    target_markets: list[MarketRow],
    knobs: dict[str, Any],
    now: datetime,
    ledger_once: Callable[..., None],
) -> list[_Eligible]:
    """Stage 1: skip retired/inactive/backed-off/held markets, ledger rejects, keep the eligible."""
    eligible: list[_Eligible] = []
    for market in target_markets:
        market_id = market.market_id
        if vault.is_market_inactive(market_id, dir_key="filters"):
            log.info("[PHASE 2] skip %s: filter marked inactive", market_id)
            continue
        prior = vault.read_filter_log(market_id)
        if gate.is_retired(prior) or not gate.is_due(prior, now):
            continue

        with market_quarantine(vault, market_id, "phase2"):
            entry = _gate_market(vault, market, prior, knobs, now, ledger_once)
            if entry is not None:
                eligible.append(entry)
    return eligible


def _triage_verdict(
    vault: ObsidianManager,
    runner: AgentRunner,
    market: MarketRow,
    prior: dict[str, Any] | None,
    event_id: Any,
    today: str,
    now: datetime,
    event_cache: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Cached (per market, then per event) or fresh Triage verdict; ``None`` after an agent error."""
    market_id = market.market_id
    triage = (prior or {}).get("triage")
    if not (isinstance(triage, dict) and "researchable" in triage):
        triage = event_cache.get(str(event_id)) if event_id else None
    if triage is not None:
        return triage
    parsed = run_structured_or_flag(
        vault, market_id, "phase2", runner, "triage", _triage_payload(market, today),
        lambda err: {
            **(prior or {}), "market_id": market_id, "passed": False,
            "confidence_multiplier": 0.0, "details": err,
        },
    )
    if parsed is None:
        return None
    triage = {
        "researchable": bool(parsed.get("researchable")),
        "category": parsed.get("category"),
        "reason": parsed.get("reason"),
        "triaged_at": now.isoformat(),
    }
    if event_id:
        event_cache[str(event_id)] = triage
    return triage


def _rank_and_triage(
    vault: ObsidianManager,
    runner: AgentRunner,
    eligible: list[_Eligible],
    today: str,
    now: datetime,
    event_cache: dict[str, dict[str, Any]],
) -> list[_Researchable]:
    """Stage 2: rank by gate score, cut at the gate budget, Triage the rest."""
    eligible.sort(key=lambda item: (-item[0], item[1].market_id))
    budget = gate_budget()
    researchable: list[_Researchable] = []

    for rank, (sc, market, prior) in enumerate(eligible):
        market_id = market.market_id
        md = market.market_data
        q = scraper.select_q(md)
        fields = _ledger_market_fields(md)
        base = {
            "confidence_multiplier": sc,
            "score": sc,
            "event_id": fields["event_id"],
            **gate.register_pass(q),
        }
        if rank >= budget:
            _write_filter_state(
                vault, market_id, prior, passed=False, trigger="over_gate_budget",
                details="eligible; below OPENCLAW_GATE_BUDGET cut", **base,
            )
            continue

        with market_quarantine(vault, market_id, "phase2"):
            triage = _triage_verdict(
                vault, runner, market, prior, fields["event_id"], today, now, event_cache
            )
            if triage is None:
                continue
            if not triage.get("researchable"):
                _write_filter_state(
                    vault, market_id, prior, passed=False, trigger="triage_reject",
                    details=f"triage_reject: {triage.get('reason')}",
                    status=gate.STATUS_RETIRED, triage=triage, **base,
                )
                _ledger_safe(
                    vault, market_id, "triage_reject",
                    reason=triage.get("reason"), q=q,
                    **{**fields, "category": triage.get("category") or fields["category"]},
                )
                continue
            researchable.append((sc, market, prior, {**base, "triage": triage}))
    return researchable


def _promote_researchable(
    vault: ObsidianManager, researchable: list[_Researchable]
) -> None:
    """Stage 3: the top ``OPENCLAW_TRIAGE_BUDGET`` researchable markets get ``passed: true``."""
    for idx, (sc, market, prior, base) in enumerate(researchable):
        with market_quarantine(vault, market.market_id, "phase2"):
            promoted = idx < triage_budget()
            _write_filter_state(
                vault, market.market_id, prior, passed=promoted,
                trigger=None if promoted else "over_triage_budget",
                details="researchable" if promoted else "researchable; below OPENCLAW_TRIAGE_BUDGET cut",
                **base,
            )


def phase2_quantitative_routing(
    vault: ObsidianManager,
    target_markets: list[MarketRow],
    runner: AgentRunner = spawn_agent,
) -> None:
    """Deterministic gate -> rank -> Triage; persist state to ``01_Filters/``.

    1. Skip markets that are retired, inactive, not yet due (backoff), already
       in research, or holding an open bet.
    2. Arbitrage (yes+no ask < 0.98) is ledgered (paper only) and not researched.
    3. ``gate.eligibility``: failures are ledgered once per state change and
       back off (5h * 2^(n-1), retired after 6).
    4. Eligible markets are ranked by ``gate.score``; the top
       ``OPENCLAW_GATE_BUDGET`` go to Triage (verdict cached forever, once per event).
    5. The top ``OPENCLAW_TRIAGE_BUDGET`` researchable markets get ``passed: true``
       (``confidence_multiplier`` = score), which is all Phase 3 reads.
    """
    log.info("[PHASE 2] Gate + triage")
    knobs = extract_filter_directives(vault.read_directives())
    now = datetime.now(timezone.utc)
    today = f"{now:%Y-%m-%d}"
    last_decision = ledger.latest_decision_by_market(vault)
    event_cache = _event_triage_cache(vault)
    ledger_once = _make_ledger_once(vault, last_decision)

    eligible = _eligible_markets(vault, target_markets, knobs, now, ledger_once)
    researchable = _rank_and_triage(vault, runner, eligible, today, now, event_cache)
    _promote_researchable(vault, researchable)

    log.info(
        "[PHASE 2] gate complete: eligible=%d triaged_researchable=%d promoted=%d",
        len(eligible), len(researchable), min(len(researchable), triage_budget()),
    )
