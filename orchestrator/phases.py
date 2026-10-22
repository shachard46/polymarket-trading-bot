"""Six pipeline phases. Each phase is a pure function over its inputs.

A phase:

1. Builds the input payload for the appropriate agent.
2. Calls the injected ``runner`` to spawn the agent.
3. Parses the response (JSON/YAML for most agents; markdown for the Deep
   Researcher) using :mod:`orchestrator.parse` and :mod:`orchestrator.research`.
4. Validates and writes the payload via :class:`obsidian_utils.ObsidianManager`.
5. Flags the market inactive in place on any error or parse failure.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import ValidationError

from obsidian_utils import FilterLogPayload, ObsidianManager, VaultWriteError
from config.trading_constants import (
    AUTO_REVERT_BRIER_WORSE_BY,
    AUTO_REVERT_MIN_SCORED,
    BANKROLL_USD,
    BELOW_EDGE_KEY,
    BOUNDS,
    FILTERS,
    KNOB_COOLDOWN_VERSIONS,
    LIQ_FRAC,
    RESEARCH_MAX_QUICK_WIDTH,
    RESEARCH_MIN_TTL_HOURS,
    RESEARCH_REFRESH_PRICE_MOVE,
    RESEARCH_SAMPLES,
)
from orchestrator import gate, ledger, polymarket_api, scraper, stats
from orchestrator.config import (
    OVERSEER_INTERVAL_SEC,
    PAPER_TRADE_MODE,
    overseer_min_new_resolved,
    full_research_budget,
    gate_budget,
    quick_research_budget,
    research_tick_budget_sec,
    research_workers,
    top_qualitative_markets,
    triage_budget,
)
from orchestrator.state import (
    InfraUnavailable,
    flag_inactive,
    is_error_free_active,
    is_inactive,
    market_quarantine,
    vault_write_or_flag,
)
from orchestrator.directives import changed_knobs, effective_knobs, extract_filter_directives
from orchestrator.scraper import (
    MarketRow,
    market_data_hydration_error,
    market_data_pricing_error,
)
import agents_blueprint  # module import (not from-import) avoids a cycle when agents_blueprint is imported first
from orchestrator.agent_outputs import (
    BrieferOutput,
    DeepResearcherComplete,
    DeepResearcherNeedsMore,
    parse_deep_researcher_json,
)
from orchestrator.aiq_bundle import fetch_research_bundle, fetch_research_bundle_cached
from orchestrator.live_prompt import today_date
from orchestrator.config import FORCED_SYNTHESIS_OVERRIDE, MAX_RESEARCH_ITERATIONS
from orchestrator.parse import (
    AgentOutputParseError,
    agent_error_reason,
    coerce_deep_researcher_state_machine,
    normalize_structured_output,
    parse_agent_json_or_yaml,
    preprocess_agent_text,
    run_agent_with_format_retries,
)
from orchestrator.research import parse_deep_researcher
from orchestrator.runner import AgentRunner, spawn_agent
from orchestrator.schema_validation import AgentSchemaError

log = logging.getLogger(__name__)

Phase3Kind = Literal["initial", "refresh"]


@dataclass(frozen=True)
class Phase3Candidate:
    """One market eligible for the qualitative pipeline."""

    market_id: str
    kind: Phase3Kind
    filter_record: dict[str, Any]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


MAX_POST_MORTEM_ATTEMPTS = 3
PHASE6_MAX_POST_MORTEMS = 15
PHASE6_MAX_STATS_CHARS = 12 * 1024
PHASE6_MAX_POST_MORTEM_CHARS = 5 * 1024
PHASE6_MAX_TOTAL_CHARS = 90 * 1024

_ROOT = Path(__file__).resolve().parents[1]
_ALLOCATION_SKILL_PATH = (
    _ROOT / "skills/calculate-trade-allocation/calculate_trade_allocation.py"
)
_allocation_fn: Callable[..., Any] | None = None
_TRADE_SKILL_PATH = (
    _ROOT / "skills/execute-polymarket-trade/execute_polymarket_trade.py"
)
_trade_fn: Callable[..., Any] | None = None
_ledger_lock = threading.Lock()  # FULL-stage markets append to one ledger file concurrently


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


def _qualitative_rank_key(candidate: Phase3Candidate) -> tuple[float, str]:
    ev = candidate.filter_record
    try:
        cm = float(ev.get("confidence_multiplier", 0.0))
    except (TypeError, ValueError):
        cm = 0.0
    return (-cm, candidate.market_id)


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


def _run_structured_agent(
    runner: AgentRunner,
    role: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Spawn a JSON/YAML agent. Returns ``(parsed, error_reason)``."""
    try:
        raw = runner(role, payload)
    except AgentSchemaError as exc:
        return None, f"{role} output schema mismatch: {exc.cause}"
    try:
        parsed = parse_agent_json_or_yaml(raw)
    except AgentOutputParseError as exc:
        return None, f"{role} parse error: {exc}"
    spec = agents_blueprint.AGENTS[role]
    parsed = normalize_structured_output(
        role,
        payload,
        parsed,
        output_schema=spec.get("output_schema"),
    )
    err = agent_error_reason(parsed)
    if err:
        return parsed, f"{role} error: {err}"
    return parsed, None


def _ledger_market_fields(market_data: dict[str, Any] | None) -> dict[str, Any]:
    md = market_data or {}
    return {
        "category": md.get("category"),
        "end_date": md.get("end_date"),
        "event_id": md.get("event_id"),
    }


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


# ---------------------------------------------------------------------------
# Phase 1 — Data ingestion
# ---------------------------------------------------------------------------


def phase1_data_ingestion(vault: ObsidianManager) -> list[MarketRow]:
    """Query Polymarket (Gamma) for open binary candidates inside the days window."""
    log.info("[PHASE 1] Data ingestion")
    target_markets = scraper.fetch_target_markets(
        knobs=extract_filter_directives(vault.read_directives())
    )
    log.info(
        "[PHASE 1] target_markets count=%d ids=%r",
        len(target_markets),
        [m.market_id for m in target_markets],
    )
    return target_markets


# ---------------------------------------------------------------------------
# Phase 2 — Quantitative routing
# ---------------------------------------------------------------------------


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

    def ledger_once(market_id: str, stage: str, **fields: Any) -> None:
        if last_decision.get(market_id, {}).get("stage") == stage:
            return
        last_decision[market_id] = _ledger_safe(vault, market_id, stage, **fields) or {
            "stage": stage
        }

    # -- Stage 1: eligibility ------------------------------------------------
    eligible: list[tuple[float, MarketRow, dict[str, Any] | None]] = []
    for market in target_markets:
        market_id = market.market_id
        if vault.is_market_inactive(market_id, dir_key="filters"):
            log.info("[PHASE 2] skip %s: filter marked inactive", market_id)
            continue
        prior = vault.read_filter_log(market_id)
        if gate.is_retired(prior) or not gate.is_due(prior, now):
            continue

        with market_quarantine(vault, market_id, "phase2"):
            trade = vault.read_trade_log_dict(market_id)
            if trade is not None and _open_trade_shows_bet_not_edge_dq(trade):
                log.info("[PHASE 2] skip %s: open trade log shows an active bet", market_id)
                continue
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
                continue

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
                continue

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
                continue

            eligible.append((gate.score(md, knobs), market, prior))

    # -- Stage 2: rank, budget, triage ---------------------------------------
    eligible.sort(key=lambda item: (-item[0], item[1].market_id))
    budget = gate_budget()
    researchable: list[tuple[float, MarketRow, dict[str, Any] | None, dict[str, Any]]] = []

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
            event_id = fields["event_id"]
            triage = (prior or {}).get("triage")
            if not (isinstance(triage, dict) and "researchable" in triage):
                triage = event_cache.get(str(event_id)) if event_id else None
            if triage is None:
                parsed, err = _run_structured_agent(
                    runner, "triage", _triage_payload(market, today)
                )
                if err:
                    flag_inactive(
                        vault, market_id, "phase2", err,
                        {**(prior or {}), "market_id": market_id, "passed": False,
                         "confidence_multiplier": 0.0, "details": err},
                    )
                    continue
                triage = {
                    "researchable": bool(parsed.get("researchable")),
                    "category": parsed.get("category"),
                    "reason": parsed.get("reason"),
                    "triaged_at": now.isoformat(),
                }
                if event_id:
                    event_cache[str(event_id)] = triage

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

    for idx, (sc, market, prior, base) in enumerate(researchable):
        with market_quarantine(vault, market.market_id, "phase2"):
            promoted = idx < triage_budget()
            _write_filter_state(
                vault, market.market_id, prior, passed=promoted,
                trigger=None if promoted else "over_triage_budget",
                details="researchable" if promoted else "researchable; below OPENCLAW_TRIAGE_BUDGET cut",
                **base,
            )

    log.info(
        "[PHASE 2] gate complete: eligible=%d triaged_researchable=%d promoted=%d",
        len(eligible), len(researchable), min(len(researchable), triage_budget()),
    )


# ---------------------------------------------------------------------------
# Phase 3 — Research (decoupled vault scan): QUICK stage, Hub decision, FULL stage
#
# The models are price-blind: no payload built here contains q. The Hub compares
# the model's (p, p_low, p_high) with q and decides skip / escalate / write.
# ---------------------------------------------------------------------------

_SKIP_OUTCOMES = frozenset({"no_divergence", "sample_disagreement", "probable_misread"})
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def research_ttl_hours(days_to_resolution: float | None) -> float:
    """Refresh TTL: ``max(48h, days_to_resolution * 24h / 10)``."""
    days = days_to_resolution if days_to_resolution is not None else 0.0
    return max(RESEARCH_MIN_TTL_HOURS, float(days) * 24.0 / 10.0)


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


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


def build_phase3_queue(
    vault: ObsidianManager, now: datetime | None = None
) -> list[Phase3Candidate]:
    """Scan ``01_Filters/`` for gate+triage survivors (``passed: true``) needing research.

    - ``initial``: no error-free research and no still-valid skip/cooldown from an
      earlier attempt (``research_state``).
    - ``refresh``: error-free research older than its TTL, or flagged by Phase 2
      (``refresh_reason``: price moved > 8 points since ``q_at_research``). Markets
      holding an open bet are never refreshed.
    """
    now = now or datetime.now(timezone.utc)
    candidates: list[Phase3Candidate] = []

    for path in vault.iter_dir_files("filters", ".md"):
        market_id = path.stem
        record = vault.read_market_record(market_id, "filters")
        if record is None or is_inactive(record) or gate.is_retired(record):
            continue
        if not record.get("passed"):
            continue
        state = record.get("research_state")
        fm = _error_free_active_fm(vault, market_id)
        if fm is None:
            if (
                isinstance(state, dict)
                and state.get("outcome") in _SKIP_OUTCOMES
                and _attempt_cooldown_active(state, _as_float(record.get("last_q")), now)
            ):
                continue
            kind: Phase3Kind = "initial"
        else:
            trade = vault.read_trade_log_dict(market_id)
            if trade is not None and _open_trade_shows_bet_not_edge_dq(trade):
                continue
            if record.get("refresh_reason") != "price_move" and not _refresh_reason(
                vault, market_id, fm, state, None, now
            ):
                continue
            kind = "refresh"
        candidates.append(
            Phase3Candidate(market_id=market_id, kind=kind, filter_record=record)
        )

    candidates.sort(key=_qualitative_rank_key)
    cap = top_qualitative_markets()
    if len(candidates) > cap:
        log.info(
            "[PHASE 3 queue] capping qualitative queue: %d -> %d (OPENCLAW_TOP_MARKETS)",
            len(candidates),
            cap,
        )
        return candidates[:cap]
    return candidates


@dataclass
class _Job:
    """One market selected for the FULL stage."""

    candidate: Phase3Candidate
    row: dict[str, Any]

    @property
    def market_id(self) -> str:
        return self.candidate.market_id

    @property
    def refresh(self) -> bool:
        return self.candidate.kind == "refresh"

    @property
    def event_id(self) -> str | None:
        md = self.row.get("market_data") or {}
        event_id = md.get("event_id") or self.candidate.filter_record.get("event_id")
        return str(event_id) if event_id else None


def quick_decision(
    p_low: float, p_high: float, q: float, max_width: float | None = None
) -> Literal["no_divergence", "escalate"]:
    """Hub decision after the QUICK stage: skip only when q sits inside a narrow range.

    ``max_width`` is the directives knob ``research_max_quick_width`` (default constant).
    """
    inside = p_low <= q <= p_high
    width = RESEARCH_MAX_QUICK_WIDTH if max_width is None else max_width
    if inside and (p_high - p_low) <= width:
        return "no_divergence"
    return "escalate"


def aggregate_samples(
    samples: list[tuple[DeepResearcherComplete, Any]],
) -> dict[str, Any]:
    """Combine independent FULL-stage samples.

    ``p``, ``p_low`` and ``p_high`` are each the **median** across samples (min/max
    would let one outlier widen the range and are dominated by sample noise; the
    spread guard already rejects disagreement). Medians of ordered triples keep
    ``p_low <= p <= p_high``. ``chosen`` is the sample closest to the median ``p``
    (its markdown body is the one written); ``confidence`` is the most conservative.
    """
    ps = [c.estimated_p for c, _ in samples]
    p = statistics.median(ps)
    chosen = min(range(len(ps)), key=lambda i: (abs(ps[i] - p), i))
    confidence = min((c.confidence for c, _ in samples), key=lambda c: _CONFIDENCE_RANK[c])
    return {
        "p": p,
        "p_low": statistics.median(c.p_low for c, _ in samples),
        "p_high": statistics.median(c.p_high for c, _ in samples),
        "samples": ps,
        "spread": max(ps) - min(ps),
        "confidence": confidence,
        "chosen": chosen,
    }


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
        **_ledger_market_fields(row.get("market_data")),
    )
    _record_attempt(vault, row, stage, reason, **values)
    log.info("[PHASE 3] %s research_skip %s for %s", stage, reason, market_id)


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


def _bundle_row_usable(entry: dict[str, Any]) -> bool:
    return bool(str(entry.get("research_data") or "").strip()) and not entry.get("error")


def _bundle_has_entries(bundle: list[dict[str, Any]] | None) -> bool:
    """True only when at least one row carries data and no error (not a poisoned bundle)."""
    return any(_bundle_row_usable(e) for e in bundle or [])


def _ensure_as_of(query: str, today: str) -> str:
    return query if f"as of {today}" in query.lower() else f"{query.rstrip()} (as of {today})"


def _plan_research_queries(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    today: str,
) -> list[str] | None:
    market_id = row["market_id"]
    brief_in = _text_fields(row, today)
    spec = agents_blueprint.AGENTS["briefer"]

    def _validate_briefer(parsed: dict[str, Any]) -> BrieferOutput:
        normalized = normalize_structured_output(
            "briefer",
            brief_in,
            parsed,
            output_schema=spec.get("output_schema"),
        )
        return BrieferOutput.model_validate(normalized)

    try:
        brief = run_agent_with_format_retries(
            runner, "briefer", brief_in, validate_fn=_validate_briefer
        )
    except ValidationError as exc:
        flag_inactive(
            vault,
            market_id,
            "phase3",
            f"briefer output validation failed: {exc}",
            brief_in,
        )
        return None

    err = agent_error_reason(brief.model_dump())
    if err:
        flag_inactive(vault, market_id, "phase3", f"briefer error: {err}", brief.model_dump())
        return None

    return [_ensure_as_of(q, today) for q in brief.research_queries]


def _aiq_cache_path(vault: ObsidianManager) -> Path:
    return vault._dirs["active"] / "aiq_cache.json"


def _fetch_and_persist_bundle(
    vault: ObsidianManager,
    market_id: str,
    queries: list[str],
) -> None:
    cleaned = [str(q).strip() for q in queries if str(q).strip()]
    if not cleaned:
        return
    results = fetch_research_bundle_cached(
        cleaned, _aiq_cache_path(vault), fetch=fetch_research_bundle
    )
    vault.write_research_bundle(market_id, results)


def _invoke_deep_researcher(
    runner: AgentRunner,
    dr_in: dict[str, Any],
) -> DeepResearcherComplete | DeepResearcherNeedsMore:
    payload = dict(dr_in)

    def _parse_dr_response(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            base: Any = raw
        elif isinstance(raw, str):
            try:
                base = parse_agent_json_or_yaml(preprocess_agent_text(raw))
            except AgentOutputParseError:
                base = raw
        else:
            base = raw
        return coerce_deep_researcher_state_machine(base, payload)

    def _validate_dr(parsed: dict[str, Any]) -> DeepResearcherComplete | DeepResearcherNeedsMore:
        normalized = normalize_structured_output(
            "deep_researcher",
            payload,
            parsed,
            output_schema=None,
        )
        return parse_deep_researcher_json(normalized)

    return run_agent_with_format_retries(
        runner,
        "deep_researcher",
        payload,
        validate_fn=_validate_dr,
        parse_response=_parse_dr_response,
    )


def _check_complete(
    vault: ObsidianManager,
    market_id: str,
    complete: DeepResearcherComplete,
    *,
    flag: bool = True,
) -> Any | None:
    """Cross-check JSON vs markdown frontmatter. Returns the parsed research or ``None``.

    ``flag=False`` (extra samples) only logs: one bad sample must not blacklist a market.
    """

    def fail(reason: str, payload: dict[str, Any]) -> None:
        if flag:
            flag_inactive(vault, market_id, "phase3", reason, payload)
        else:
            log.warning("[PHASE 3] dropping sample for %s: %s", market_id, reason)

    try:
        research = parse_deep_researcher(complete.markdown)
    except (ValueError, AgentOutputParseError) as exc:
        fail(
            f"deep researcher markdown validation failed: {exc}",
            {"markdown": complete.markdown[:500]},
        )
        return None

    if research.error:
        fail(
            f"deep researcher error in frontmatter: {research.error}",
            research.frontmatter,
        )
        return None

    if complete.market_id != market_id:
        fail(
            f"deep researcher returned mismatched market_id: {complete.market_id!r}",
            {"expected": market_id, "got": complete.market_id},
        )
        return None

    if abs(complete.estimated_p - research.estimated_p) > 1e-6:
        fail(
            "deep researcher estimated_p mismatch between JSON and markdown frontmatter",
            {
                "json_estimated_p": complete.estimated_p,
                "markdown_estimated_p": research.estimated_p,
            },
        )
        return None

    for name, json_val, md_val in (
        ("p_low", complete.p_low, research.p_low),
        ("p_high", complete.p_high, research.p_high),
    ):
        if md_val is not None and abs(json_val - md_val) > 1e-6:
            fail(
                f"deep researcher {name} mismatch between JSON and markdown frontmatter",
                {f"json_{name}": json_val, f"markdown_{name}": md_val},
            )
            return None

    if research.market_id is not None and research.market_id != market_id:
        fail(
            f"deep researcher markdown market_id mismatch: {research.market_id!r}",
            research.frontmatter,
        )
        return None

    return research


def _quick_stage(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    directives: str,
    today: str,
) -> Literal["escalate", "skip", "failed"]:
    """One price-blind Deep Researcher call with no A-IQ bundle, then the Hub decision."""
    market_id = row["market_id"]
    md = row.get("market_data") or {}
    q = scraper.select_q(md)
    dr_in = _dr_payload(
        row, directives, [], mode="quick", today=today,
        system_override=FORCED_SYNTHESIS_OVERRIDE, session_tag="quick",
    )
    try:
        out = _invoke_deep_researcher(runner, dr_in)
    except ValidationError as exc:
        flag_inactive(
            vault, market_id, "phase3",
            f"deep researcher quick-stage output validation failed: {exc}", dr_in,
        )
        return "failed"
    if isinstance(out, DeepResearcherNeedsMore):
        flag_inactive(
            vault, market_id, "phase3",
            "deep researcher disobeyed quick-stage override", out.model_dump(),
        )
        return "failed"
    if _check_complete(vault, market_id, out) is None:
        return "failed"

    values = {"q": q, "p": out.estimated_p, "p_low": out.p_low, "p_high": out.p_high}
    knobs = extract_filter_directives(directives)
    if quick_decision(out.p_low, out.p_high, q, knobs["research_max_quick_width"]) == "no_divergence":
        _skip_research(vault, row, "quick", "no_divergence", **values)
        return "skip"
    _record_attempt(vault, row, "quick", "escalate", **values)
    return "escalate"


def _research_loop(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    directives: str,
    today: str,
    tag: str,
) -> DeepResearcherComplete | None:
    """Lead FULL sample: one round, one follow-up round, then forced synthesis."""
    market_id = row["market_id"]
    last_out: DeepResearcherComplete | DeepResearcherNeedsMore | None = None

    for _ in range(MAX_RESEARCH_ITERATIONS):
        dr_in = _dr_payload(
            row, directives, vault.read_research_bundle(market_id) or [],
            mode="full", today=today, session_tag=tag,
        )
        try:
            last_out = _invoke_deep_researcher(runner, dr_in)
        except ValidationError as exc:
            flag_inactive(
                vault, market_id, "phase3",
                f"deep researcher output validation failed: {exc}", dr_in,
            )
            return None
        if isinstance(last_out, DeepResearcherComplete):
            return last_out
        _fetch_and_persist_bundle(vault, market_id, list(last_out.new_queries))

    override_in = _dr_payload(
        row, directives, vault.read_research_bundle(market_id) or [],
        mode="full", today=today, system_override=FORCED_SYNTHESIS_OVERRIDE,
        session_tag=tag,
    )
    try:
        forced = _invoke_deep_researcher(runner, override_in)
    except ValidationError as exc:
        flag_inactive(
            vault, market_id, "phase3",
            f"deep researcher forced synthesis validation failed: {exc}", override_in,
        )
        return None
    if isinstance(forced, DeepResearcherNeedsMore):
        flag_inactive(
            vault, market_id, "phase3",
            "deep researcher disobeyed forced synthesis override", forced.model_dump(),
        )
        return None
    return forced


def _extra_sample(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    directives: str,
    today: str,
    bundle: list[dict[str, Any]],
    tag: str,
) -> tuple[DeepResearcherComplete, Any] | None:
    """Independent re-estimate on the lead sample's final bundle (own session, no fetches)."""
    market_id = row["market_id"]
    dr_in = _dr_payload(
        row, directives, bundle, mode="full", today=today,
        system_override=FORCED_SYNTHESIS_OVERRIDE, session_tag=tag,
    )
    try:
        out = _invoke_deep_researcher(runner, dr_in)
    except ValidationError as exc:
        log.warning("[PHASE 3] sample %s for %s failed validation: %s", tag, market_id, exc)
        return None
    if isinstance(out, DeepResearcherNeedsMore):
        log.warning("[PHASE 3] sample %s for %s ignored the override", tag, market_id)
        return None
    research = _check_complete(vault, market_id, out, flag=False)
    return (out, research) if research is not None else None


def _research_market(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    directives: str,
    *,
    refresh: bool = False,
    shared_bundle: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """FULL stage for one market: Briefer -> A-IQ -> Deep Researcher x3 -> Hub decision.

    Returns the Phase 4 row (``p_value``/``p_low``/``p_high``/``market_data``) when active
    research was written, ``None`` after a ledgered skip or an inactive flag.
    """
    market_id = row["market_id"]
    md = row.get("market_data") or {}
    q = scraper.select_q(md)
    today = today_date()

    if shared_bundle:  # same-event sibling: reuse the first market's evidence
        vault.clear_research_bundle(market_id)
        vault.write_research_bundle(market_id, shared_bundle)
    elif refresh or not _bundle_has_entries(vault.read_research_bundle(market_id)):
        if refresh:
            vault.clear_research_bundle(market_id)
        queries = _plan_research_queries(vault, runner, row, today)
        if queries is None:
            return None
        _fetch_and_persist_bundle(vault, market_id, queries)

    lead = _research_loop(vault, runner, row, directives, today, "s1")
    if lead is None:
        return None
    lead_research = _check_complete(vault, market_id, lead)
    if lead_research is None:
        return None
    samples: list[tuple[DeepResearcherComplete, Any]] = [(lead, lead_research)]

    bundle = vault.read_research_bundle(market_id) or []
    for k in range(2, RESEARCH_SAMPLES + 1):
        extra = _extra_sample(vault, runner, row, directives, today, bundle, f"s{k}")
        if extra is not None:
            samples.append(extra)
    if len(samples) < 2:
        flag_inactive(
            vault, market_id, "phase3",
            "fewer than 2 valid deep researcher samples", {"valid": len(samples)},
        )
        return None

    agg = aggregate_samples(samples)
    values = {
        "q": q, "p": agg["p"], "p_low": agg["p_low"], "p_high": agg["p_high"],
        "samples": agg["samples"], "spread": agg["spread"], "confidence": agg["confidence"],
    }
    knobs = extract_filter_directives(directives)
    if agg["spread"] > knobs["research_max_sample_spread"]:
        _skip_research(vault, row, "full", "sample_disagreement", **values)
        return None
    if q is not None and abs(agg["p"] - q) > knobs["research_max_q_gap"]:
        _skip_research(vault, row, "full", "probable_misread", **values)
        return None

    chosen, chosen_research = samples[agg["chosen"]]
    payload = {
        "market_id": market_id,
        "estimated_p": agg["p"],
        "error": None,
        "p_low": agg["p_low"],
        "p_high": agg["p_high"],
        "confidence": agg["confidence"],
        "as_of": chosen.as_of.isoformat(),
        "resolution_checklist": chosen.resolution_checklist,
        "key_assumptions": chosen.key_assumptions,
        "q_at_research": q,  # Hub-side only (refresh trigger); never sent to a model
        "researched_at": datetime.now(timezone.utc).isoformat(),
        "end_date": md.get("end_date"),
        "days_at_research": _as_float(md.get("days_to_resolution")),
        "samples": agg["samples"],
    }
    if not vault_write_or_flag(
        vault=vault,
        market_id=market_id,
        write_fn=lambda: vault.write_research_report(market_id, payload, chosen_research.body),
        payload=payload,
        artifact_label="research report",
        phase="phase3",
    ):
        return None
    _record_attempt(vault, row, "full", "researched", **values)
    return {
        "market_id": market_id,
        "p_value": agg["p"],
        "p_low": agg["p_low"],
        "p_high": agg["p_high"],
        "market_data": md,
    }


def _run_full_stage(
    vault: ObsidianManager,
    runner: AgentRunner,
    directives: str,
    jobs: list[_Job],
    over_budget: Callable[[], bool],
) -> list[dict[str, Any]]:
    """Run FULL research for ``jobs``: events in parallel, same-event markets sequentially.

    Markets sharing an ``event_id`` reuse the first market's evidence bundle (the Deep
    Researcher still runs per market). Vault writes are per-market atomic files.
    """
    if not jobs:
        return []
    groups: dict[Any, list[_Job]] = {}
    for job in jobs:
        groups.setdefault(job.event_id or ("solo", job.market_id), []).append(job)

    abort = threading.Event()

    def run_group(group: list[_Job]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        shared: list[dict[str, Any]] | None = None
        for job in group:
            if abort.is_set():
                break
            if over_budget():
                log.info(
                    "[PHASE 3] tick budget exhausted; not starting %s", job.market_id
                )
                continue
            result = None
            try:
                with market_quarantine(vault, job.market_id, "phase3"):
                    result = _research_market(
                        vault, runner, job.row, directives,
                        refresh=job.refresh, shared_bundle=shared,
                    )
            except InfraUnavailable:
                abort.set()
                raise
            if shared is None:
                leader_bundle = vault.read_research_bundle(job.market_id)
                if _bundle_has_entries(leader_bundle):
                    shared = leader_bundle
            if result is not None:
                out.append(result)
        return out

    results: list[dict[str, Any]] = []
    infra: BaseException | None = None
    with ThreadPoolExecutor(max_workers=research_workers()) as pool:
        futures = [pool.submit(run_group, g) for g in groups.values()]
        for fut in futures:
            try:
                results.extend(fut.result())
            except InfraUnavailable as exc:
                infra = infra or exc
    if infra is not None:
        raise infra
    return results


def phase3_qualitative_pipeline(
    vault: ObsidianManager,
    runner: AgentRunner = spawn_agent,
) -> list[dict[str, Any]]:
    """QUICK stage -> Hub decision -> FULL stage; persist research to ``02_Active_Research/``.

    Candidates are processed in gate-score order. QUICK (<= ``OPENCLAW_QUICK_RESEARCH_BUDGET``
    markets) is one price-blind Deep Researcher call without A-IQ data; the Hub skips markets
    where q lies inside a narrow ``[p_low, p_high]`` and escalates the rest. FULL
    (<= ``OPENCLAW_FULL_RESEARCH_BUDGET``) is Briefer -> A-IQ -> 3 Deep Researcher samples.
    Refresh candidates go straight to FULL. No new market starts after
    ``OPENCLAW_RESEARCH_TICK_BUDGET_SEC``.
    """
    log.info("[PHASE 3] Research pipeline")
    directives = vault.read_directives()
    knobs = extract_filter_directives(directives)
    queue = build_phase3_queue(vault)
    started = time.monotonic()
    budget_sec = research_tick_budget_sec()

    def over_budget() -> bool:
        return time.monotonic() - started >= budget_sec

    now = datetime.now(timezone.utc)
    today = today_date()
    quick_left = quick_research_budget()
    to_full: list[_Job] = []

    for candidate in queue:
        market_id = candidate.market_id
        if over_budget():
            log.info("[PHASE 3] tick budget exhausted; stopping the QUICK scan")
            break
        row = scraper.fetch_market_row(market_id)
        if row is None:
            log.warning(
                "[PHASE 3] skip %s: could not hydrate market row from scraper",
                market_id,
            )
            continue

        pricing_err = market_data_pricing_error(row.market_data)
        if pricing_err:
            # Transient data gap: skip this tick, never blacklist.
            log.warning("[PHASE 3] skip %s this tick: %s", market_id, pricing_err)
            continue

        market_row = row.model_dump()
        q = scraper.select_q(row.market_data)
        with market_quarantine(vault, market_id, "phase3"):
            if candidate.kind == "refresh":
                ok, why = gate.eligibility(row.market_data, knobs)
                if not ok:
                    log.info("[PHASE 3] skip refresh of %s: %s", market_id, why)
                    continue
                to_full.append(_Job(candidate, market_row))
                continue
            state = candidate.filter_record.get("research_state")
            if (
                isinstance(state, dict)
                and state.get("stage") == "quick"
                and state.get("outcome") == "escalate"
                and _attempt_cooldown_active(state, q, now)
            ):
                to_full.append(_Job(candidate, market_row))  # quick already escalated it
                continue
            if quick_left <= 0:
                continue
            quick_left -= 1
            if _quick_stage(vault, runner, market_row, directives, today) == "escalate":
                to_full.append(_Job(candidate, market_row))

    selected = to_full[: full_research_budget()]
    if len(to_full) > len(selected):
        log.info(
            "[PHASE 3] FULL budget: %d escalated, %d deferred to the next tick",
            len(to_full), len(to_full) - len(selected),
        )
    researched = _run_full_stage(vault, runner, directives, selected, over_budget)
    log.info("[PHASE 3] researched count=%d", len(researched))
    return researched


def phase3_research_market(
    vault: ObsidianManager,
    market_id: str,
    runner: AgentRunner = spawn_agent,
    *,
    kind: Phase3Kind = "initial",
) -> dict[str, Any] | None:
    """Run the FULL stage for one market without scanning ``01_Filters/`` (no QUICK stage)."""
    log.info("[PHASE 3] research single market market_id=%s kind=%s", market_id, kind)
    row = scraper.fetch_market_row(market_id)
    if row is None:
        log.warning(
            "[PHASE 3] skip %s: could not hydrate market row from scraper",
            market_id,
        )
        return None

    pricing_err = market_data_pricing_error(row.market_data)
    if pricing_err:
        log.warning("[PHASE 3] skip %s this tick: %s", market_id, pricing_err)
        return None

    directives = vault.read_directives()
    with market_quarantine(vault, market_id, "phase3"):
        return _research_market(
            vault, runner, row.model_dump(), directives, refresh=(kind == "refresh")
        )
    return None


# ---------------------------------------------------------------------------
# Phase 4 — Execution
# ---------------------------------------------------------------------------


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
                    "market_data": fresh.market_data,
                },
                False,
            )
        )
    return queue


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
        market_id = row["market_id"]
        market_data = row.get("market_data") or {}

        with market_quarantine(vault, market_id, "phase4"):
            if vault.has_execution_intent(market_id):
                log.error(
                    "[PHASE 4] refusing %s: execution intent exists from an earlier "
                    "attempt (outcome unknown); resolve manually and delete the intent",
                    market_id,
                )
                continue
            existing = vault.read_trade_log_dict(market_id)
            if vault.market_file(market_id, "trades") is not None:
                # Only an edge-disqualified trade from a fresh research refresh may re-execute.
                if not (
                    from_research
                    and existing is not None
                    and _trade_log_shows_edge_disqualification(existing)
                ):
                    log.info("[PHASE 4] skip %s: trade log already exists", market_id)
                    continue

            q = scraper.select_q(market_data)
            if q is None:
                log.warning("[PHASE 4] skip %s this tick: no usable q", market_id)
                continue
            hydration_err = market_data_hydration_error(market_data)
            if hydration_err:
                log.warning("[PHASE 4] skip %s this tick: %s", market_id, hydration_err)
                continue

            quotes = _phase4_quotes(market_id)
            if quotes is None:
                log.warning("[PHASE 4] skip %s this tick: executable quote unavailable", market_id)
                continue

            p_value, p_low, p_high = row["p_value"], row.get("p_low"), row.get("p_high")
            allocation = compute_allocation(
                vault, p_value, q, market_data, p_low, p_high, quotes=quotes
            )
            if allocation.get("error"):
                log.warning(
                    "[PHASE 4] skip %s this tick: allocation error: %s",
                    market_id,
                    allocation["error"],
                )
                continue

            size_usd = float(allocation["allocation_usd"])
            below = bool(allocation["below_edge_threshold"]) or not size_usd > 0
            bought = quotes.get(allocation.get("side") or "") or {}
            trade: dict[str, Any] = {
                "market_id": market_id,
                "allocation_usd": size_usd,
                "score": allocation.get("edge"),
                "below_edge_threshold": below,
                "executed": False,
                "transaction_hash": None,
                "error": None,
                "p": p_value,
                "p_low": p_low,
                "p_high": p_high,
                "q_entry": q,
                "side": allocation.get("side"),
                "price": allocation.get("price"),
                "ask": bought.get("ask"),
                "depth_usd": bought.get("depth_usd_within_2c"),
                "shares": allocation.get("shares"),
                "directive_version": ledger.current_directive_version(vault),
                "event_id": market_data.get("event_id"),
            }

            if not below:
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
                    continue
                if res.get("paper"):
                    log.info("[PAPER_TRADE] %s %s $%.2f", market_id, allocation["side"], size_usd)
                trade["executed"] = bool(res.get("success")) and not res.get("paper")
                trade["transaction_hash"] = res.get("transaction_hash")

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
                    p=p_value,
                    p_low=p_low,
                    p_high=p_high,
                    side=allocation.get("side"),
                    price=allocation.get("price"),
                    ask=bought.get("ask"),
                    depth_usd=bought.get("depth_usd_within_2c"),
                    shares=allocation.get("shares"),
                    size_usd=size_usd,
                    **_ledger_market_fields(market_data),
                )


# ---------------------------------------------------------------------------
# Phase 5 — Resolution & post-mortem
# ---------------------------------------------------------------------------


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
                vault, market_id, outcome_yes, (resolution or {}).get("raw_outcome"), None
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
    parsed, reason = _run_structured_agent(runner, "post_mortem_analyst", payload)
    if reason:
        _post_mortem_failed(vault, market_id, reason, parsed or payload)
        return

    try:
        vault.append_post_mortem(market_id, parsed)
    except VaultWriteError as exc:
        _post_mortem_failed(
            vault,
            market_id,
            f"post-mortem append validation failed: {exc.cause}",
            parsed,
        )
        return

    _ledger_safe(
        vault, market_id, ledger.POST_MORTEM_STAGE,
        failure_class=(parsed.get("failure_class") or "unclassified"),
        outcome_yes=payload["outcome_yes"],
        pnl_usd=payload["realised_pnl_usd"],
        p=payload["p"], side=payload["side"], price=payload["price"],
        size_usd=payload["size_usd"], category=payload["category"],
    )
    try:
        vault.archive_trade(market_id)
    except FileNotFoundError:
        log.warning("Trade log already archived for %s", market_id)


# ---------------------------------------------------------------------------
# Phase 6 — Macro-learning (Overseer)
# ---------------------------------------------------------------------------


def _bounded_post_mortems(vault: ObsidianManager) -> list[dict[str, Any]]:
    """Most recent post-mortems by mtime, each truncated, within a total size cap."""
    paths = sorted(vault.iter_post_mortems(), key=lambda p: p.stat().st_mtime, reverse=True)
    batch: list[dict[str, Any]] = []
    total = 0
    for path in paths[:PHASE6_MAX_POST_MORTEMS]:
        content = path.read_text(encoding="utf-8")[:PHASE6_MAX_POST_MORTEM_CHARS]
        if total + len(content) > PHASE6_MAX_TOTAL_CHARS:
            break
        total += len(content)
        batch.append({"market_id": path.stem, "content": content})
    dropped = len(paths) - len(batch)
    if dropped:
        log.info(
            "[PHASE 6] sending %d of %d post-mortems (%d dropped by size/count caps)",
            len(batch),
            len(paths),
            dropped,
        )
    return batch


def _directives_frontmatter(vault: ObsidianManager) -> dict[str, Any]:
    from orchestrator.research import split_yaml_frontmatter_markdown

    text = vault.read_directives()
    try:
        return split_yaml_frontmatter_markdown(text)[0] if text.strip() else {}
    except ValueError:
        return {}


def _bounds_payload(vault: ObsidianManager, current_md: str) -> list[dict[str, Any]]:
    """The Overseer-facing bounds table: per knob current/default/min/max/step/cooldown."""
    knobs = effective_knobs(current_md)
    fm = _directives_frontmatter(vault)
    klc = fm.get("knob_last_changed") if isinstance(fm.get("knob_last_changed"), dict) else {}
    next_version = vault.latest_directives_version() + 1
    rows = []
    for key, b in BOUNDS.items():
        try:
            at = int(klc.get(key))
        except (TypeError, ValueError):
            at = None
        rows.append({
            "knob": key,
            "current": knobs[key],
            "default": FILTERS[key],
            "min": b["min"],
            "max": b["max"],
            "max_step_frac": b["max_step_frac"],
            "last_changed_version": at,
            "locked": at is not None and next_version - at < KNOB_COOLDOWN_VERSIONS,
        })
    return rows


def _auto_revert_if_worse(vault: ObsidianManager, rows: list[dict[str, Any]]) -> bool:
    """Restore the previous directives version when the current one scores clearly worse.

    Compares model Brier (stats helpers grouped by ledger ``directive_version``) once the current
    version has ``AUTO_REVERT_MIN_SCORED`` resolved scored markets (and so does the previous one).
    A version that is itself a restore is never reverted (no flip-flopping).
    """
    current = ledger.current_directive_version(vault)
    previous = current - 1
    fm = _directives_frontmatter(vault)
    if previous < 1 or fm.get("restored_from") is not None:
        return False
    if vault.read_directives_version(previous) is None:
        return False
    by_version = stats.brier_by_directive_version(rows)
    n_cur, b_cur = by_version.get(current, (0, 0.0))
    n_prev, b_prev = by_version.get(previous, (0, 0.0))
    if n_cur < AUTO_REVERT_MIN_SCORED or n_prev < AUTO_REVERT_MIN_SCORED:
        return False
    if b_cur - b_prev <= AUTO_REVERT_BRIER_WORSE_BY:
        return False
    reason = (
        f"auto-revert: v{current} Brier {b_cur:.4f} (n={n_cur}) is worse than "
        f"v{previous} {b_prev:.4f} (n={n_prev}) by more than {AUTO_REVERT_BRIER_WORSE_BY}"
    )
    new_version = vault.restore_directives(previous, reason)
    vault.patch_directives_frontmatter({"last_overseer_at": datetime.now(timezone.utc).isoformat()})
    log.error("[PHASE 6] !!! DIRECTIVES AUTO-REVERTED: %s -> restored as v%d; Overseer skipped",
              reason, new_version)
    return True


def phase6_macro_learning_loop(
    vault: ObsidianManager,
    runner: AgentRunner = spawn_agent,
) -> None:
    """Stats + failure classes + post-mortems -> Overseer -> bounded, versioned directives.

    Order: (1) auto-revert a version that scores clearly worse than its predecessor (and skip the
    Overseer); (2) run only if >= ``OVERSEER_MIN_NEW_RESOLVED`` new resolution rows exist since the
    last run and ``OVERSEER_INTERVAL_SEC`` has passed; (3) a proposal that violates the bounds is
    rejected whole and the old directives stay.
    """
    log.info("[PHASE 6] Macro-learning loop (Overseer)")
    rows = list(ledger.iter_rows(vault))
    if _auto_revert_if_worse(vault, rows):
        return

    fm = _directives_frontmatter(vault)
    now = datetime.now(timezone.utc)
    last_at = gate._parse_ts(fm.get("last_overseer_at"))
    if last_at is not None and (now - last_at).total_seconds() < OVERSEER_INTERVAL_SEC:
        log.info("[PHASE 6] skipped: last Overseer run %s is under the minimum interval", last_at)
        return
    n_resolved = sum(1 for r in rows if r.get("stage") == ledger.RESOLUTION_STAGE)
    try:
        last_count = int(fm.get("last_overseer_ledger_count") or 0)
    except (TypeError, ValueError):
        last_count = 0
    need = overseer_min_new_resolved()
    if n_resolved - last_count < need:
        log.info(
            "[PHASE 6] skipped: %d new resolutions since the last Overseer run (need %d)",
            n_resolved - last_count, need,
        )
        return

    try:
        stats.write_stats(vault)
    except Exception:  # noqa: BLE001 - fall back to computing in memory
        log.exception("[PHASE 6] write_stats failed")
    stats_path = vault._dirs["system"] / "stats.md"
    stats_md = stats_path.read_text(encoding="utf-8") if stats_path.exists() else ""
    current = vault.read_directives()
    payload = {
        "post_mortems": _bounded_post_mortems(vault),
        "current_directives": current,
        "stats_md": stats_md[:PHASE6_MAX_STATS_CHARS],
        "failure_class_counts": stats.failure_class_counts(rows),
        "bounds": _bounds_payload(vault, current),
    }
    parsed, reason = _run_structured_agent(runner, "overseer", payload)
    if reason:
        log.error("Overseer failed: %s — keeping prior directives", reason)
        return

    from orchestrator.research import split_yaml_frontmatter_markdown

    proposal = parsed.get("new_directives_markdown")
    unchanged = False
    try:
        unchanged = (
            not changed_knobs(current, proposal)
            and split_yaml_frontmatter_markdown(current)[1]
            == split_yaml_frontmatter_markdown(proposal)[1]
        )
    except (ValueError, TypeError, AttributeError):
        pass  # malformed: let write_directives reject it
    if not unchanged:
        try:
            vault.write_directives(parsed)
        except VaultWriteError as exc:
            log.error(
                "Directives rejected: %s — keeping prior directives (v%d)",
                exc.cause, ledger.current_directive_version(vault),
            )
            return
    else:
        log.info("[PHASE 6] Overseer proposed no change; keeping v%d", ledger.current_directive_version(vault))
    vault.patch_directives_frontmatter(
        {"last_overseer_ledger_count": n_resolved, "last_overseer_at": now.isoformat()}
    )


__all__ = [
    "Phase3Candidate",
    "Phase3Kind",
    "phase1_data_ingestion",
    "phase2_quantitative_routing",
    "build_phase3_queue",
    "research_ttl_hours",
    "quick_decision",
    "aggregate_samples",
    "phase3_qualitative_pipeline",
    "phase3_research_market",
    "compute_allocation",
    "phase4_execution",
    "phase5_resolution_and_post_mortem",
    "phase6_macro_learning_loop",
]
