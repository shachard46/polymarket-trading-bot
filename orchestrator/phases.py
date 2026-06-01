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

from obsidian_utils import ObsidianManager, VaultWriteError
from config.trading_constants import PENDING_EDGE_REFRESH_KEY
from orchestrator import scraper
from orchestrator.config import PAPER_TRADE_MODE, max_edge_research_refreshes, top_qualitative_markets
from orchestrator.state import (
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
from orchestrator.parse import (
    AgentOutputParseError,
    agent_error_reason,
    coerce_deep_researcher_markdown,
    normalize_structured_output,
    parse_agent_json_or_yaml,
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
    bet = data.get("below_edge_threshold")
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
    bet = data.get("below_edge_threshold")
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
        with market_quarantine(vault, market_id, "phase2"):
            trade = vault.read_trade_log_dict(market_id)
            if trade is not None and _open_trade_shows_bet_not_edge_dq(trade):
                log.info(
                    "[PHASE 2] skip %s: open trade log shows an active bet",
                    market_id,
                )
                continue

            has_active = vault.active_research_path(market_id).exists()
            prior_full = vault.read_filter_log(market_id)
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
                    vault.patch_frontmatter(
                        market_id,
                        "filters",
                        {PENDING_EDGE_REFRESH_KEY: False},
                    )
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

            if not parsed.get("passed"):
                log.info("Market %s did not pass quantitative filters", market_id)
                continue

            if not vault_write_or_flag(
                vault=vault,
                market_id=market_id,
                write_fn=lambda: vault.write_filter_log(market_id, parsed),
                payload=parsed,
                artifact_label="filter log",
                phase="phase2",
            ):
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
    """Deep Researcher only — overwrite active research; strip pending flag."""
    market_id = row["market_id"]
    summary = _context_summary_from_active(vault, market_id)
    if not summary:
        brief_in = {
            "market_id": market_id,
            "market_title": row.get("market_title", ""),
            "market_description": row.get("market_description", ""),
        }
        brief, reason = _run_structured_agent(runner, "briefer", brief_in)
        if reason:
            flag_inactive(vault, market_id, "phase3", reason, brief or brief_in)
            return None
        summary = brief.get("summary")
        if not summary:
            flag_inactive(vault, market_id, "phase3", "briefer returned no summary", brief)
            return None

    result = _run_deep_researcher(
        vault, runner, row, directives, summary, market_id, from_edge=True
    )
    if result is None:
        return None

    vault.strip_keys(market_id, "filters", (PENDING_EDGE_REFRESH_KEY,))
    return result


def _context_summary_from_active(vault: ObsidianManager, market_id: str) -> str | None:
    """Use Bull thesis excerpt from existing research as context when re-briefing."""
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
    """Brief + research a single market. Returns row for phase 4 or None on failure."""
    market_id = row["market_id"]

    brief_in = {
        "market_id": market_id,
        "market_title": row.get("market_title", ""),
        "market_description": row.get("market_description", ""),
    }
    brief, reason = _run_structured_agent(runner, "briefer", brief_in)
    if reason:
        flag_inactive(vault, market_id, "phase3", reason, brief or brief_in)
        return None
    summary = brief.get("summary")
    if not summary:
        flag_inactive(vault, market_id, "phase3", "briefer returned no summary", brief)
        return None

    return _run_deep_researcher(
        vault, runner, row, directives, summary, market_id, from_edge=False
    )


def _run_deep_researcher(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    directives: str,
    summary: str,
    market_id: str,
    *,
    from_edge: bool,
) -> dict[str, Any] | None:
    dr_in = {
        "market_id": market_id,
        "market_data": row.get("market_data") or {},
        "context_summary": summary,
        "directives": directives,
    }
    raw_dr = runner("deep_researcher", dr_in)
    try:
        markdown = coerce_deep_researcher_markdown(raw_dr)
        research = parse_deep_researcher(markdown)
    except (AgentOutputParseError, ValueError) as exc:
        flag_inactive(
            vault,
            market_id,
            "phase3",
            f"deep researcher parse error: {exc}",
            {"raw": str(raw_dr)},
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

    if research.market_id is not None and research.market_id != market_id:
        flag_inactive(
            vault,
            market_id,
            "phase3",
            f"deep researcher returned mismatched market_id: {research.market_id!r}",
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
