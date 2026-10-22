"""Phase 3 entrypoints: candidate queue, QUICK scan, parallel FULL stage and the tick budget."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from obsidian_utils import ObsidianManager
from orchestrator import gate, scraper
from orchestrator.config import (
    full_research_budget,
    quick_research_budget,
    research_tick_budget_sec,
    research_workers,
    top_qualitative_markets,
)
from orchestrator.directives import extract_filter_directives
from orchestrator.live_prompt import today_date
from orchestrator.phases import p3_full  # module access: tests patch ``p3_full._research_market``
from orchestrator.phases.common import (
    Phase3Candidate,
    Phase3Kind,
    _as_float,
    _open_trade_shows_bet_not_edge_dq,
    log,
)
from orchestrator.phases.p3_evidence import _bundle_has_entries
from orchestrator.phases.p3_quick import _quick_stage
from orchestrator.phases.research_state import (
    _attempt_cooldown_active,
    _error_free_active_fm,
    _refresh_reason,
)
from orchestrator.runner import AgentRunner, spawn_agent
from orchestrator.scraper import market_data_pricing_error
from orchestrator.state import InfraUnavailable, is_inactive, market_quarantine

_SKIP_OUTCOMES = frozenset({"no_divergence", "sample_disagreement", "probable_misread"})


def _qualitative_rank_key(candidate: Phase3Candidate) -> tuple[float, str]:
    ev = candidate.filter_record
    try:
        cm = float(ev.get("confidence_multiplier", 0.0))
    except (TypeError, ValueError):
        cm = 0.0
    return (-cm, candidate.market_id)


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
        shared_meta: dict[str, Any] | None = None
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
                    result = p3_full._research_market(
                        vault, runner, job.row, directives,
                        refresh=job.refresh, shared_bundle=shared, shared_meta=shared_meta,
                    )
            except InfraUnavailable:
                abort.set()
                raise
            if shared is None:
                leader_bundle = vault.read_research_bundle(job.market_id)
                if _bundle_has_entries(leader_bundle):
                    shared = leader_bundle
                    shared_meta = vault.read_research_bundle_meta(job.market_id)
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
        return p3_full._research_market(
            vault, runner, row.model_dump(), directives, refresh=(kind == "refresh")
        )
    return None
