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

import logging
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from obsidian_utils import ObsidianManager, VaultWriteError
from config.trading_constants import (
    BELOW_EDGE_KEY,
    ERROR_LOG_KEY,
    PENDING_EDGE_REFRESH_KEY,
)
from orchestrator import scraper
from orchestrator.config import PAPER_TRADE_MODE, max_edge_research_refreshes, top_qualitative_markets
from orchestrator.state import (
    build_inactive_filter_payload,
    flag_inactive,
    has_pending_edge_refresh,
    is_error_free_active,
    is_inactive,
    market_quarantine,
    read_edge_refresh_count,
    vault_write_or_flag,
)
from orchestrator.directives import extract_filter_directives
from orchestrator.scraper import MarketRow
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
    normalize_structured_output,
    parse_agent_json_or_yaml,
    run_agent_with_format_retries,
)
from orchestrator.research import parse_deep_researcher, split_yaml_frontmatter_markdown
from orchestrator.evaluator_output import attach_signal_bundle
from orchestrator.runner import AgentRunner, spawn_agent
from orchestrator.schema_validation import AgentSchemaError

log = logging.getLogger(__name__)

Phase3Kind = Literal["initial", "edge_refresh"]


@dataclass(frozen=True)
class Phase3Candidate:
    """One market eligible for the qualitative pipeline."""

    market_id: str
    kind: Phase3Kind
    filter_record: dict[str, Any]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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
    """True when the last run hit the edge gate (no allocation due to score vs ``S_0``)."""
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
    parsed = attach_signal_bundle(role, payload, parsed)
    err = agent_error_reason(parsed)
    if err:
        return parsed, f"{role} error: {err}"
    return parsed, None


def _persist_phase2_filter(
    vault: ObsidianManager,
    market_id: str,
    parsed: dict[str, Any],
) -> bool:
    """Persist quantitative agent output; stamp inactive when ``passed`` is false."""
    if not parsed.get("passed"):
        reason = str(
            parsed.get("details") or "did not pass quantitative filters"
        )
        body = build_inactive_filter_payload(parsed, "phase2", reason)
        body[ERROR_LOG_KEY] = {
            **body[ERROR_LOG_KEY],
            "trigger": parsed.get("trigger"),
            "details": parsed.get("details"),
        }
        log.info(
            "Market %s did not pass quantitative filters (persisted inactive)",
            market_id,
        )
    else:
        body = parsed
    return vault_write_or_flag(
        vault=vault,
        market_id=market_id,
        write_fn=lambda: vault.write_filter_log(market_id, body),
        payload=body,
        artifact_label="filter log",
        phase="phase2",
    )


def _write_filter_with_pending_refresh(
    vault: ObsidianManager,
    market_id: str,
    parsed: dict[str, Any],
    *,
    pending_edge_refresh: bool,
) -> bool:
    payload = {**parsed, PENDING_EDGE_REFRESH_KEY: pending_edge_refresh}
    return vault_write_or_flag(
        vault=vault,
        market_id=market_id,
        write_fn=lambda: vault.write_filter_log(market_id, payload),
        payload=payload,
        artifact_label="filter log",
        phase="phase2",
    )


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


def phase2_quantitative_routing(
    vault: ObsidianManager,
    target_markets: list[MarketRow],
    runner: AgentRunner = spawn_agent,
) -> None:
    """Filter markets through the (re-)Evaluator; persist to ``01_Filters/``."""
    log.info("[PHASE 2] Quantitative routing")
    filter_directives = extract_filter_directives(vault.read_directives())

    for market in target_markets:
        market_id = market.market_id
        if vault.is_market_inactive(market_id, dir_key="filters"):
            log.info("[PHASE 2] skip %s: filter marked inactive", market_id)
            continue
        prior_full = vault.read_filter_log(market_id)

        with market_quarantine(vault, market_id, "phase2"):
            trade = vault.read_trade_log_dict(market_id)
            if trade is not None and _open_trade_shows_bet_not_edge_dq(trade):
                log.info(
                    "[PHASE 2] skip %s: open trade log shows an active bet",
                    market_id,
                )
                continue

            has_active = vault.active_research_path(market_id).exists()
            historic_signal_bundle = (
                prior_full.get("signal_bundle") if prior_full else None
            )

            if (
                has_active
                and trade is not None
                and _trade_log_shows_edge_disqualification(trade)
            ):
                cap_ref = max_edge_research_refreshes()
                prev_edge = read_edge_refresh_count(vault, market_id)
                if prev_edge >= cap_ref:
                    log.info(
                        "[PHASE 2] skip edge research refresh for %s (cap %s)",
                        market_id,
                        cap_ref,
                    )
                    continue

                prior_trigger = prior_full.get("trigger") if prior_full else None
                prior_details = prior_full.get("details") if prior_full else None
                research_md = vault.read_active_research(market_id) or ""
                payload_re: dict[str, Any] = {
                    "market_id": market_id,
                    "review_kind": "edge_research_refresh",
                    "filter_directives": filter_directives,
                    "historic_signal_bundle": historic_signal_bundle,
                    "prior_filter_trigger": prior_trigger,
                    "prior_evaluator_details": prior_details,
                    "prior_filter_log": prior_full,
                    "research_markdown": research_md,
                    "trade_log": trade,
                }
                parsed_re, reason_re = _run_structured_agent(
                    runner, "re_evaluator", payload_re
                )
                if reason_re:
                    flag_inactive(
                        vault, market_id, "phase2", reason_re, parsed_re or payload_re
                    )
                    continue
                if parsed_re.get("retry_deep_research"):
                    if not _write_filter_with_pending_refresh(
                        vault,
                        market_id,
                        parsed_re,
                        pending_edge_refresh=True,
                    ):
                        continue
                else:
                    if not _write_filter_with_pending_refresh(
                        vault,
                        market_id,
                        parsed_re,
                        pending_edge_refresh=False,
                    ):
                        continue
                continue

            role = "re_evaluator" if has_active else "evaluator"
            if role == "evaluator":
                payload: dict[str, Any] = {
                    "market_id": market_id,
                    "filter_directives": filter_directives,
                }
            else:
                prior = prior_full
                payload = {
                    "market_id": market_id,
                    "review_kind": "quantitative",
                    "filter_directives": filter_directives,
                    "historic_signal_bundle": historic_signal_bundle,
                    "prior_filter_trigger": prior.get("trigger") if prior else None,
                    "prior_evaluator_details": prior.get("details") if prior else None,
                    "prior_filter_log": None,
                    "research_markdown": None,
                    "trade_log": None,
                }

            parsed, reason = _run_structured_agent(runner, role, payload)
            if reason:
                flag_inactive(vault, market_id, "phase2", reason, parsed or payload)
                continue

            if not _persist_phase2_filter(vault, market_id, parsed):
                continue

    log.info("[PHASE 2] quantitative routing complete")


# ---------------------------------------------------------------------------
# Phase 3 — Qualitative pipeline (decoupled vault scan)
# ---------------------------------------------------------------------------


def build_phase3_queue(vault: ObsidianManager) -> list[Phase3Candidate]:
    """Scan ``01_Filters/`` for initial research or ``pending_edge_refresh`` work."""
    cap_ref = max_edge_research_refreshes()
    candidates: list[Phase3Candidate] = []

    for path in vault.iter_dir_files("filters", ".md"):
        market_id = path.stem
        record = vault.read_market_record(market_id, "filters")
        if record is None or is_inactive(record):
            continue

        if has_pending_edge_refresh(record):
            if not is_error_free_active(vault, market_id):
                continue
            if read_edge_refresh_count(vault, market_id) >= cap_ref:
                continue
            candidates.append(
                Phase3Candidate(
                    market_id=market_id,
                    kind="edge_refresh",
                    filter_record=record,
                )
            )
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


def _edge_refresh_research(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    directives: str,
    _candidate: Phase3Candidate,
) -> dict[str, Any] | None:
    """Iterative research refresh — overwrite active research; strip pending flag."""
    market_id = row["market_id"]
    planning_context = _planning_context_from_active(vault, market_id)
    result = _run_iterative_research(
        vault,
        runner,
        row,
        directives,
        from_edge=True,
        planning_context=planning_context,
    )
    if result is None:
        return None

    vault.strip_keys(market_id, "filters", (PENDING_EDGE_REFRESH_KEY,))
    return result


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


def _bundle_has_entries(bundle: list[dict[str, Any]] | None) -> bool:
    return bool(bundle)


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
) -> None:
    cleaned = [str(q).strip() for q in queries if str(q).strip()]
    if not cleaned:
        return
    results = fetch_research_bundle(cleaned)
    vault.write_research_bundle(market_id, results)


def _invoke_deep_researcher(
    runner: AgentRunner,
    dr_in: dict[str, Any],
) -> DeepResearcherComplete | DeepResearcherNeedsMore:
    payload = dict(dr_in)

    def _validate_dr(parsed: dict[str, Any]) -> DeepResearcherComplete | DeepResearcherNeedsMore:
        normalized = normalize_structured_output(
            "deep_researcher",
            payload,
            parsed,
            output_schema=None,
        )
        return parse_deep_researcher_json(normalized)

    return run_agent_with_format_retries(
        runner, "deep_researcher", payload, validate_fn=_validate_dr
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

    if _bundle_has_entries(existing_bundle):
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
            _fetch_and_persist_bundle(vault, market_id, pending_queries)

        bundle = vault.read_research_bundle(market_id) or []
        dr_in: dict[str, Any] = {
            "market_id": market_id,
            "market_data": row.get("market_data") or {},
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
        bundle = vault.read_research_bundle(market_id) or []
        dr_in = {
            "market_id": market_id,
            "market_data": row.get("market_data") or {},
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


def phase4_execution(
    vault: ObsidianManager,
    researched_markets: list[dict[str, Any]],
    runner: AgentRunner = spawn_agent,
) -> None:
    """Spawn the Trade Executioner; honor :data:`PAPER_TRADE_MODE`."""
    log.info("[PHASE 4] Execution")
    if PAPER_TRADE_MODE:
        log.info(
            "[PAPER_TRADE] forcing executed=False and transaction_hash=None on trade logs"
        )

    for row in researched_markets:
        market_id = row["market_id"]
        with market_quarantine(vault, market_id, "phase4"):
            payload = {
                "market_id": market_id,
                "p_value": row["p_value"],
                "market_data": row.get("market_data") or {},
                "paper_trade_mode": bool(PAPER_TRADE_MODE),
            }
            parsed, reason = _run_structured_agent(runner, "executioner", payload)
            if reason:
                flag_inactive(vault, market_id, "phase4", reason, parsed or payload)
                continue

            if PAPER_TRADE_MODE:
                parsed = {**parsed, "executed": False, "transaction_hash": None}

            vault_write_or_flag(
                vault=vault,
                market_id=market_id,
                write_fn=lambda: vault.write_trade_log(market_id, parsed),
                payload=parsed,
                artifact_label="trade log",
                phase="phase4",
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


def _resolve_market(
    vault: ObsidianManager,
    runner: AgentRunner,
    market_id: str,
) -> None:
    """Resolve one market and append post-mortem analysis when possible."""
    resolution = scraper.fetch_resolution(market_id)
    if resolution is None:
        return

    try:
        vault.move_file(market_id, "active", "post_mortem")
    except FileNotFoundError:
        log.warning("No active research file to move for %s", market_id)
        return

    payload = {
        "market_id": market_id,
        "original_research": vault.read_post_mortem(market_id),
        "execution_log": vault.read_trade_log(market_id),
        "resolution_data": resolution,
    }
    parsed, reason = _run_structured_agent(runner, "post_mortem_analyst", payload)
    if reason:
        flag_inactive(vault, market_id, "phase5", reason, parsed or payload)
        return

    if not vault_write_or_flag(
        vault=vault,
        market_id=market_id,
        write_fn=lambda: vault.append_post_mortem(market_id, parsed),
        payload=parsed,
        artifact_label="post-mortem append",
        phase="phase5",
    ):
        return

    try:
        vault.archive_trade(market_id)
    except FileNotFoundError:
        log.warning("Trade log already archived for %s", market_id)


# ---------------------------------------------------------------------------
# Phase 6 — Macro-learning (Overseer)
# ---------------------------------------------------------------------------


def phase6_macro_learning_loop(
    vault: ObsidianManager,
    runner: AgentRunner = spawn_agent,
) -> None:
    """Aggregate post-mortems → Overseer → overwrite ``active_directives.md``."""
    log.info("[PHASE 6] Macro-learning loop (Overseer)")
    batch = [
        {"market_id": p.stem, "content": p.read_text(encoding="utf-8")}
        for p in vault.iter_post_mortems()
    ]
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
    "phase4_execution",
    "phase5_resolution_and_post_mortem",
    "phase6_macro_learning_loop",
]
