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
import json
import logging
import os
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import ValidationError

from obsidian_utils import FilterLogPayload, ObsidianManager, VaultWriteError
from config.trading_constants import (
    BANKROLL_USD,
    BELOW_EDGE_KEY,
)
from orchestrator import gate, ledger, scraper, stats
from orchestrator.config import PAPER_TRADE_MODE, gate_budget, top_qualitative_markets, triage_budget
from orchestrator.state import (
    flag_inactive,
    is_error_free_active,
    is_inactive,
    market_quarantine,
    read_edge_refresh_attempts,
    read_edge_refresh_count,
    vault_write_or_flag,
)
from orchestrator.directives import extract_filter_directives
from orchestrator.scraper import (
    MarketRow,
    market_data_hydration_error,
    market_data_pricing_error,
)
from agents_blueprint import AGENTS
from orchestrator.agent_outputs import (
    BrieferOutput,
    DeepResearcherComplete,
    DeepResearcherNeedsMore,
    parse_deep_researcher_json,
)
from orchestrator.aiq_bundle import fetch_research_bundle
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
from orchestrator.research import parse_deep_researcher, split_yaml_frontmatter_markdown
from orchestrator.signal_bundle import fetch_signal_bundle
from orchestrator.runner import AgentRunner, spawn_agent
from orchestrator.schema_validation import AgentSchemaError

log = logging.getLogger(__name__)

# TODO(wave3): re-add an "edge_refresh" kind when Phase 3 refresh is redone.
Phase3Kind = Literal["initial"]


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
PHASE6_MAX_POST_MORTEMS = 30
PHASE6_MAX_POST_MORTEM_CHARS = 5 * 1024
PHASE6_MAX_TOTAL_CHARS = 90 * 1024

_ROOT = Path(__file__).resolve().parents[1]
_ALLOCATION_SKILL_PATH = (
    _ROOT / "skills/calculate-trade-allocation/calculate_trade_allocation.py"
)
_allocation_fn: Callable[..., Any] | None = None


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
    spec = AGENTS[role]
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
        return ledger.record(vault, market_id, stage, **fields)
    except Exception:  # noqa: BLE001
        log.exception("ledger write failed for %s (%s)", market_id, stage)
        return None


# ---------------------------------------------------------------------------
# Phase 1 — Data ingestion
# ---------------------------------------------------------------------------


def phase1_data_ingestion(vault: ObsidianManager) -> list[MarketRow]:
    """Query polymarket-scraper for high-delta / new markets."""
    log.info("[PHASE 1] Data ingestion")
    target_markets = scraper.fetch_target_markets()
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


def _signal_bundle_or_none(market_id: str) -> dict[str, Any] | None:
    try:
        bundle = fetch_signal_bundle(market_id)
    except Exception as exc:  # noqa: BLE001 - signals are a ranking bonus only
        log.warning("[PHASE 2] signal bundle unavailable for %s: %s", market_id, exc)
        return None
    return bundle if isinstance(bundle, dict) and not bundle.get("error") else None


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
    eligible: list[tuple[float, MarketRow, dict[str, Any] | None, dict[str, Any] | None]] = []
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
            if vault.active_research_path(market_id).exists():
                # TODO(wave3): re-check researched markets here (edge/price refresh).
                continue

            md = market.market_data
            q = scraper.select_q(md)
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

            ok, reason = gate.eligibility(md)
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

            bundle = _signal_bundle_or_none(market_id)
            eligible.append((gate.score(md, bundle), market, prior, bundle))

    # -- Stage 2: rank, budget, triage ---------------------------------------
    eligible.sort(key=lambda item: (-item[0], item[1].market_id))
    budget = gate_budget()
    researchable: list[tuple[float, MarketRow, dict[str, Any] | None, dict[str, Any]]] = []

    for rank, (sc, market, prior, bundle) in enumerate(eligible):
        market_id = market.market_id
        md = market.market_data
        q = scraper.select_q(md)
        fields = _ledger_market_fields(md)
        base = {
            "confidence_multiplier": sc,
            "score": sc,
            "event_id": fields["event_id"],
            "signal_bundle": bundle,
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
# Phase 3 — Qualitative pipeline (decoupled vault scan)
# ---------------------------------------------------------------------------


def build_phase3_queue(vault: ObsidianManager) -> list[Phase3Candidate]:
    """Scan 01_Filters/ for gate+triage survivors (``passed: true``) without research.

    TODO(wave3): also queue researched markets due for a refresh.
    """
    candidates: list[Phase3Candidate] = []

    for path in vault.iter_dir_files("filters", ".md"):
        market_id = path.stem
        record = vault.read_market_record(market_id, "filters")
        if record is None or is_inactive(record) or gate.is_retired(record):
            continue
        if not record.get("passed"):
            continue
        if is_error_free_active(vault, market_id):
            continue
        candidates.append(
            Phase3Candidate(
                market_id=market_id,
                kind="initial",
                filter_record=record,
            )
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


def phase3_qualitative_pipeline(
    vault: ObsidianManager,
    runner: AgentRunner = spawn_agent,
) -> list[dict[str, Any]]:
    """Briefer → Deep Researcher; persist research to ``02_Active_Research/``."""
    log.info("[PHASE 3] Qualitative pipeline")
    directives = vault.read_directives()
    queue = build_phase3_queue(vault)
    researched: list[dict[str, Any]] = []

    for candidate in queue:
        market_id = candidate.market_id
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
        result: dict[str, Any] | None = None
        with market_quarantine(vault, market_id, "phase3"):
            if candidate.kind == "edge_refresh":
                result = _edge_refresh_research(
                    vault, runner, market_row, directives, candidate
                )
            else:
                result = _research_market(vault, runner, market_row, directives)
        if result is not None:
            researched.append(result)

    log.info("[PHASE 3] researched count=%d", len(researched))
    return researched


def phase3_research_market(
    vault: ObsidianManager,
    market_id: str,
    runner: AgentRunner = spawn_agent,
    *,
    kind: Phase3Kind = "initial",
) -> dict[str, Any] | None:
    """Run Briefer → Deep Researcher for one market without scanning ``01_Filters/``."""
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
    market_row = row.model_dump()
    with market_quarantine(vault, market_id, "phase3"):
        if kind == "edge_refresh":
            record = vault.read_market_record(market_id, "filters") or {}
            candidate = Phase3Candidate(
                market_id=market_id,
                kind="edge_refresh",
                filter_record=record,
            )
            return _edge_refresh_research(
                vault, runner, market_row, directives, candidate
            )
        return _research_market(vault, runner, market_row, directives)


def _edge_refresh_research(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    directives: str,
    _candidate: Phase3Candidate,
) -> dict[str, Any] | None:
    """Iterative research refresh — overwrite active research.

    TODO(wave3): the Phase 2 trigger (``pending_edge_refresh``) was removed; Wave 3
    redoes refresh scheduling and calls this via ``phase3_research_market``.
    """
    market_id = row["market_id"]
    planning_context = _planning_context_from_active(vault, market_id)
    return _run_iterative_research(
        vault,
        runner,
        row,
        directives,
        from_edge=True,
        planning_context=planning_context,
    )


def _planning_context_from_active(vault: ObsidianManager, market_id: str) -> str | None:
    """Use Bull thesis excerpt from existing research as Query Planner context."""
    raw = vault.read_active_research(market_id)
    if not raw:
        return None
    try:
        _, body = split_yaml_frontmatter_markdown(raw)
    except ValueError:
        return None
    header = "## Bull Thesis"
    if header not in body:
        return None
    start = body.index(header) + len(header)
    end = body.find("## Bear Thesis", start)
    excerpt = body[start:end].strip() if end > start else body[start:].strip()
    if not excerpt:
        return None
    return f"Prior research (bull excerpt):\n{excerpt[:2000]}"


def _research_market(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    directives: str,
) -> dict[str, Any] | None:
    """Plan, fetch, and research a single market. Returns row for phase 4 or None."""
    return _run_iterative_research(
        vault, runner, row, directives, from_edge=False, planning_context=None
    )


def _bundle_row_usable(entry: dict[str, Any]) -> bool:
    return bool(str(entry.get("research_data") or "").strip()) and not entry.get("error")


def _bundle_has_entries(bundle: list[dict[str, Any]] | None) -> bool:
    """True only when at least one row carries data and no error (not a poisoned bundle)."""
    return any(_bundle_row_usable(e) for e in bundle or [])


def _plan_research_queries(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    planning_context: str | None,
) -> list[str] | None:
    market_id = row["market_id"]
    brief_in: dict[str, Any] = {
        "market_id": market_id,
        "market_title": row.get("market_title", ""),
        "market_description": row.get("market_description", ""),
        "planning_context": planning_context,
    }
    spec = AGENTS["briefer"]

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

    return list(brief.research_queries)


def _fetch_and_persist_bundle(
    vault: ObsidianManager,
    market_id: str,
    queries: list[str],
    *,
    fresh: bool = False,
) -> None:
    cleaned = [str(q).strip() for q in queries if str(q).strip()]
    if not cleaned:
        return
    results = fetch_research_bundle(cleaned)
    if fresh and _bundle_has_entries(results):
        # Edge refresh: drop stale rows (the merge keeps prior rows for repeated queries).
        vault.clear_research_bundle(market_id)
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


def _finalize_research(
    vault: ObsidianManager,
    market_id: str,
    complete: DeepResearcherComplete,
    row: dict[str, Any],
    *,
    from_edge: bool,
) -> dict[str, Any] | None:
    try:
        research = parse_deep_researcher(complete.markdown)
    except (ValueError, AgentOutputParseError) as exc:
        flag_inactive(
            vault,
            market_id,
            "phase3",
            f"deep researcher markdown validation failed: {exc}",
            {"markdown": complete.markdown[:500]},
        )
        return None

    if research.error:
        flag_inactive(
            vault,
            market_id,
            "phase3",
            f"deep researcher error in frontmatter: {research.error}",
            research.frontmatter,
        )
        return None

    if complete.market_id != market_id:
        flag_inactive(
            vault,
            market_id,
            "phase3",
            f"deep researcher returned mismatched market_id: {complete.market_id!r}",
            {"expected": market_id, "got": complete.market_id},
        )
        return None

    if abs(complete.estimated_p - research.estimated_p) > 1e-6:
        flag_inactive(
            vault,
            market_id,
            "phase3",
            "deep researcher estimated_p mismatch between JSON and markdown frontmatter",
            {
                "json_estimated_p": complete.estimated_p,
                "markdown_estimated_p": research.estimated_p,
            },
        )
        return None

    if research.market_id is not None and research.market_id != market_id:
        flag_inactive(
            vault,
            market_id,
            "phase3",
            f"deep researcher markdown market_id mismatch: {research.market_id!r}",
            research.frontmatter,
        )
        return None

    prev_edge = read_edge_refresh_count(vault, market_id)
    edge_count = prev_edge + 1 if from_edge else prev_edge

    payload = {
        "market_id": market_id,
        "estimated_p": research.estimated_p,
        "error": None,
        "edge_research_refresh_count": edge_count,
        "edge_research_refresh_attempts": read_edge_refresh_attempts(vault, market_id),
    }
    if not vault_write_or_flag(
        vault=vault,
        market_id=market_id,
        write_fn=lambda: vault.write_research_report(market_id, payload, research.body),
        payload=payload,
        artifact_label="research report",
        phase="phase3",
    ):
        return None

    return {
        "market_id": market_id,
        "p_value": research.estimated_p,
        "market_data": row.get("market_data") or {},
    }


def _forced_synthesis(
    vault: ObsidianManager,
    runner: AgentRunner,
    dr_in: dict[str, Any],
    row: dict[str, Any],
    market_id: str,
    *,
    from_edge: bool,
) -> dict[str, Any] | None:
    override_in = {**dr_in, "system_override": FORCED_SYNTHESIS_OVERRIDE}
    try:
        last_out = _invoke_deep_researcher(runner, override_in)
    except ValidationError as exc:
        flag_inactive(
            vault,
            market_id,
            "phase3",
            f"deep researcher forced synthesis validation failed: {exc}",
            override_in,
        )
        return None

    if isinstance(last_out, DeepResearcherNeedsMore):
        flag_inactive(
            vault,
            market_id,
            "phase3",
            "deep researcher disobeyed forced synthesis override",
            last_out.model_dump(),
        )
        return None

    return _finalize_research(vault, market_id, last_out, row, from_edge=from_edge)


def _run_iterative_research(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    directives: str,
    *,
    from_edge: bool,
    planning_context: str | None,
) -> dict[str, Any] | None:
    market_id = row["market_id"]
    existing_bundle = vault.read_research_bundle(market_id)
    q = scraper.select_q(row.get("market_data") or {})
    fresh_fetch = from_edge

    # Edge refreshes always re-plan and refetch; otherwise reuse a usable saved bundle.
    if not from_edge and _bundle_has_entries(existing_bundle):
        pending_queries: list[str] = []
    else:
        planned = _plan_research_queries(vault, runner, row, planning_context)
        if planned is None:
            return None
        pending_queries = planned

    iteration = 0
    last_out: DeepResearcherComplete | DeepResearcherNeedsMore | None = None

    while iteration < MAX_RESEARCH_ITERATIONS:
        if pending_queries:
            _fetch_and_persist_bundle(
                vault, market_id, pending_queries, fresh=fresh_fetch
            )
            fresh_fetch = False

        bundle = vault.read_research_bundle(market_id) or []
        dr_in: dict[str, Any] = {
            "market_id": market_id,
            "market_data": row.get("market_data") or {},
            "q": q,
            "directives": directives,
            "research_bundle": bundle,
            "system_override": None,
            "format_validation_error": None,
        }

        try:
            last_out = _invoke_deep_researcher(runner, dr_in)
        except ValidationError as exc:
            flag_inactive(
                vault,
                market_id,
                "phase3",
                f"deep researcher output validation failed: {exc}",
                dr_in,
            )
            return None

        if isinstance(last_out, DeepResearcherComplete):
            return _finalize_research(
                vault, market_id, last_out, row, from_edge=from_edge
            )

        pending_queries = list(last_out.new_queries)
        iteration += 1

    if isinstance(last_out, DeepResearcherNeedsMore):
        # Fetch the queries requested by the last needs_more_data before forcing synthesis.
        if pending_queries:
            _fetch_and_persist_bundle(vault, market_id, pending_queries)
        bundle = vault.read_research_bundle(market_id) or []
        dr_in = {
            "market_id": market_id,
            "market_data": row.get("market_data") or {},
            "q": q,
            "directives": directives,
            "research_bundle": bundle,
            "system_override": None,
            "format_validation_error": None,
        }
        return _forced_synthesis(
            vault, runner, dr_in, row, market_id, from_edge=from_edge
        )

    flag_inactive(
        vault,
        market_id,
        "phase3",
        "deep researcher produced no output",
        {"iteration": iteration},
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
) -> dict[str, Any]:
    """Run ``calculate_trade_allocation`` in-process against bankroll + realised P&L."""
    try:
        days = int(float(market_data.get("days_to_resolution")))
    except (TypeError, ValueError):
        days = None  # the skill reports an input error
    event_id = market_data.get("event_id")
    bankroll = BANKROLL_USD + stats.realised_pnl(list(ledger.iter_rows(vault)))
    result = _load_calculate_trade_allocation()(
        p=p,
        q_yes_ask=q,
        D=days,
        L=market_data.get("liquidity"),
        bankroll=bankroll,
        open_exposure_total=_open_allocated_usd(vault),
        open_exposure_event=_open_allocated_usd(vault, event_id) if event_id else 0.0,
        p_low=p_low,
        p_high=p_high,
    )
    return result.model_dump() if hasattr(result, "model_dump") else dict(result)


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
                    "market_data": fresh.market_data,
                },
                False,
            )
        )
    return queue


def phase4_execution(
    vault: ObsidianManager,
    researched_markets: list[dict[str, Any]],
    runner: AgentRunner = spawn_agent,
) -> None:
    """Spawn the Trade Executioner; honor :data:`PAPER_TRADE_MODE`.

    The Hub computes the allocation and overrides the agent's allocation fields.
    An intent record guards against double execution across ticks.
    """
    log.info("[PHASE 4] Execution")
    if PAPER_TRADE_MODE:
        log.info(
            "[PAPER_TRADE] forcing executed=False and transaction_hash=None on trade logs"
        )

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

            allocation = compute_allocation(
                vault, row["p_value"], q, market_data, row.get("p_low"), row.get("p_high")
            )
            if allocation.get("error"):
                log.warning(
                    "[PHASE 4] skip %s this tick: allocation error: %s",
                    market_id,
                    allocation["error"],
                )
                continue

            payload = {
                "market_id": market_id,
                "p_value": row["p_value"],
                "q": q,
                "allocation": allocation,
                "market_data": market_data,
                "paper_trade_mode": bool(PAPER_TRADE_MODE),
            }
            vault.write_execution_intent(
                market_id,
                {
                    "market_id": market_id,
                    "started_at": time.time(),
                    "paper_trade_mode": bool(PAPER_TRADE_MODE),
                    "allocation": allocation,
                },
            )
            parsed, reason = _run_structured_agent(runner, "executioner", payload)
            if reason:
                log.error(
                    "[PHASE 4] executioner failed for %s (%s); intent left in place, "
                    "market will not be re-executed automatically",
                    market_id,
                    reason,
                )
                flag_inactive(vault, market_id, "phase4", reason, parsed or payload)
                continue

            # The Hub's numbers win; the LLM only reports whether it executed.
            parsed = {
                **parsed,
                "allocation_usd": allocation["allocation_usd"],
                "score": allocation.get("edge"),
                "below_edge_threshold": allocation["below_edge_threshold"],
            }
            if PAPER_TRADE_MODE or not allocation["allocation_usd"] > 0:
                parsed = {**parsed, "executed": False, "transaction_hash": None}

            size_usd = float(allocation["allocation_usd"])
            parsed = {
                **parsed,
                "p": row["p_value"],
                "q_entry": q,
                "side": allocation.get("side"),
                "price": allocation.get("price"),
                "shares": allocation.get("shares"),
                "p_low": row.get("p_low"),
                "p_high": row.get("p_high"),
                "directive_version": ledger.current_directive_version(vault),
                "event_id": market_data.get("event_id"),
            }
            if vault_write_or_flag(
                vault=vault,
                market_id=market_id,
                write_fn=lambda: vault.write_trade_log(market_id, parsed),
                payload=parsed,
                artifact_label="trade log",
                phase="phase4",
            ):
                vault.clear_execution_intent(market_id)
                below = bool(allocation["below_edge_threshold"]) or not size_usd > 0
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
                vault, market_id, outcome_yes, (resolution or {}).get("outcome"), None
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

    payload = {
        "market_id": market_id,
        "original_research": vault.read_post_mortem(market_id),
        "execution_log": vault.read_trade_log(market_id),
        "resolution_data": resolution,
    }
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


def phase6_macro_learning_loop(
    vault: ObsidianManager,
    runner: AgentRunner = spawn_agent,
) -> None:
    """Aggregate post-mortems → Overseer → overwrite ``active_directives.md``."""
    log.info("[PHASE 6] Macro-learning loop (Overseer)")
    batch = _bounded_post_mortems(vault)
    payload = {
        "post_mortems": batch,
        "current_directives": vault.read_directives(),
    }
    parsed, reason = _run_structured_agent(runner, "overseer", payload)
    if reason:
        log.error("Overseer failed: %s — keeping prior directives", reason)
        return

    try:
        vault.write_directives(parsed)
    except VaultWriteError as exc:
        log.error(
            "Directives validation failed: %s — keeping prior directives",
            exc.cause,
        )


__all__ = [
    "Phase3Candidate",
    "Phase3Kind",
    "phase1_data_ingestion",
    "phase2_quantitative_routing",
    "build_phase3_queue",
    "phase3_qualitative_pipeline",
    "phase3_research_market",
    "compute_allocation",
    "phase4_execution",
    "phase5_resolution_and_post_mortem",
    "phase6_macro_learning_loop",
]
