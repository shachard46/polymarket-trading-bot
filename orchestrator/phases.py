"""Six pipeline phases. Each phase is a pure function over its inputs.

A phase:

1. Builds the input payload for the appropriate agent.
2. Calls the injected ``runner`` to spawn the agent.
3. Parses the response (JSON/YAML for most agents; markdown for the Deep
   Researcher) using :mod:`orchestrator.parse` and :mod:`orchestrator.research`.
4. Validates and writes the payload via :class:`obsidian_utils.ObsidianManager`.
5. Quarantines the market on any error or parse failure.
"""

from __future__ import annotations

import logging
from typing import Any

from obsidian_utils import ObsidianManager, VaultWriteError
from orchestrator import scraper
from orchestrator.config import PAPER_TRADE_MODE, max_edge_research_refreshes
from orchestrator.dead_letter import (
    market_quarantine,
    quarantine_market,
    vault_write_or_quarantine,
)
from orchestrator.scraper import MarketRow
from orchestrator.parse import (
    AgentOutputParseError,
    agent_error_reason,
    coerce_deep_researcher_markdown,
    parse_agent_json_or_yaml,
)
from orchestrator.research import parse_deep_researcher, split_yaml_frontmatter_markdown
from orchestrator.runner import AgentRunner, spawn_agent

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _qualitative_rank_key(row: dict[str, Any]) -> tuple[float, str]:
    """Sort key: higher ``confidence_multiplier`` first, then ``market_id`` ascending."""
    ev = row.get("evaluator_output") or {}
    try:
        cm = float(ev.get("confidence_multiplier", 0.0))
    except (TypeError, ValueError):
        cm = 0.0
    mid = str(row.get("market_id") or "")
    return (-cm, mid)


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


def _read_edge_research_refresh_count(vault: ObsidianManager, market_id: str) -> int:
    raw = vault.read_active_research(market_id)
    if not raw:
        return 0
    try:
        fm, _ = split_yaml_frontmatter_markdown(raw)
    except ValueError:
        return 0
    try:
        return int(fm.get("edge_research_refresh_count") or 0)
    except (TypeError, ValueError):
        return 0


def merge_phase3_inputs(
    primary: list[dict[str, Any]],
    refresh_only: list[dict[str, Any]],
    cap: int,
) -> list[dict[str, Any]]:
    """Merge primary quantitative passes with edge-refresh rows; dedupe by ``market_id``."""
    by_id: dict[str, dict[str, Any]] = {}
    for row in refresh_only:
        by_id[str(row["market_id"])] = row
    for row in primary:
        by_id[str(row["market_id"])] = row
    merged = list(by_id.values())
    merged.sort(key=_qualitative_rank_key)
    if len(merged) > cap:
        log.info(
            "[PHASE 2+3 queue] capping qualitative queue: %d -> %d (OPENCLAW_TOP_MARKETS)",
            len(merged),
            cap,
        )
        return merged[:cap]
    return merged


def _run_structured_agent(
    runner: AgentRunner,
    role: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Spawn a JSON/YAML agent. Returns ``(parsed, error_reason)``.

    ``error_reason`` is non-None when the agent failed (parse error, empty
    response, or explicit ``error`` field). Callers should DLQ on that.
    """
    raw = runner(role, payload)
    try:
        parsed = parse_agent_json_or_yaml(raw)
    except AgentOutputParseError as exc:
        return None, f"{role} parse error: {exc}"
    err = agent_error_reason(parsed)
    if err:
        return parsed, f"{role} error: {err}"
    return parsed, None


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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Filter markets through the (re-)Evaluator; return ``(passed, edge_refresh_rows)``.

    Caller merges with :func:`merge_phase3_inputs` and ``top_qualitative_markets()``.
    """
    log.info("[PHASE 2] Quantitative routing")
    limit = scraper.trends_limit_for_filters()
    passed: list[dict[str, Any]] = []
    edge_refresh: list[dict[str, Any]] = []

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

            historic = scraper.get_market_trends(market_id, limit)
            has_active = vault.active_research_path(market_id).exists()

            if (
                has_active
                and trade is not None
                and _trade_log_shows_edge_disqualification(trade)
            ):
                cap_ref = max_edge_research_refreshes()
                prev_edge = _read_edge_research_refresh_count(vault, market_id)
                if prev_edge >= cap_ref:
                    log.info(
                        "[PHASE 2] skip edge research refresh for %s (cap %s)",
                        market_id,
                        cap_ref,
                    )
                    continue

                prior_full = vault.read_filter_log(market_id)
                research_md = vault.read_active_research(market_id) or ""
                prior_trigger = prior_full.get("trigger") if prior_full else None
                prior_details = prior_full.get("details") if prior_full else None
                payload_re: dict[str, Any] = {
                    "market_id": market_id,
                    "review_kind": "edge_research_refresh",
                    "historic_market_data": historic,
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
                    quarantine_market(
                        vault, market_id, reason_re, parsed_re or payload_re
                    )
                    continue
                if parsed_re.get("retry_deep_research"):
                    row_er = market.model_dump()
                    row_er["evaluator_output"] = parsed_re
                    row_er["_edge_research_refresh"] = True
                    edge_refresh.append(row_er)
                continue

            role = "re_evaluator" if has_active else "evaluator"
            if role == "evaluator":
                payload: dict[str, Any] = {
                    "market_id": market_id,
                    "historic_market_data": historic,
                }
            else:
                prior = vault.read_filter_log(market_id)
                payload = {
                    "market_id": market_id,
                    "review_kind": "quantitative",
                    "historic_market_data": historic,
                    "prior_filter_trigger": prior.get("trigger") if prior else None,
                    "prior_evaluator_details": prior.get("details") if prior else None,
                    "prior_filter_log": None,
                    "research_markdown": None,
                    "trade_log": None,
                }

            parsed, reason = _run_structured_agent(runner, role, payload)
            if reason:
                quarantine_market(vault, market_id, reason, parsed or payload)
                continue

            if not parsed.get("passed"):
                log.info("Market %s did not pass quantitative filters", market_id)
                continue

            if not vault_write_or_quarantine(
                vault=vault,
                market_id=market_id,
                write_fn=lambda: vault.write_filter_log(market_id, parsed),
                payload=parsed,
                artifact_label="filter log",
            ):
                continue

            row = market.model_dump()
            row["evaluator_output"] = parsed
            passed.append(row)

    passed.sort(key=_qualitative_rank_key)
    log.info(
        "[PHASE 2] passed_markets count=%d edge_refresh_rows=%d",
        len(passed),
        len(edge_refresh),
    )
    return passed, edge_refresh


# ---------------------------------------------------------------------------
# Phase 3 — Qualitative pipeline
# ---------------------------------------------------------------------------


def phase3_qualitative_pipeline(
    vault: ObsidianManager,
    passed_markets: list[dict[str, Any]],
    runner: AgentRunner = spawn_agent,
) -> list[dict[str, Any]]:
    """Briefer → Deep Researcher; persist research to ``02_Active_Research/``."""
    log.info("[PHASE 3] Qualitative pipeline")
    directives = vault.read_directives()
    researched: list[dict[str, Any]] = []

    for row in passed_markets:
        market_id = row["market_id"]
        researched_row: dict[str, Any] | None = None
        with market_quarantine(vault, market_id, "phase3"):
            researched_row = _research_market(vault, runner, row, directives)
        if researched_row is not None:
            researched.append(researched_row)

    log.info("[PHASE 3] researched count=%d", len(researched))
    return researched


def _research_market(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    directives: str,
) -> dict[str, Any] | None:
    """Brief + research a single market. Returns row for phase 4 or None on DLQ."""
    market_id = row["market_id"]

    brief_in = {
        "market_id": market_id,
        "market_title": row.get("market_title", ""),
        "market_description": row.get("market_description", ""),
    }
    brief, reason = _run_structured_agent(runner, "briefer", brief_in)
    if reason:
        quarantine_market(vault, market_id, reason, brief or brief_in)
        return None
    summary = brief.get("summary")
    if not summary:
        quarantine_market(vault, market_id, "briefer returned no summary", brief)
        return None

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
        quarantine_market(
            vault,
            market_id,
            f"deep researcher parse error: {exc}",
            {"raw": str(raw_dr)},
        )
        return None

    if research.error:
        quarantine_market(
            vault,
            market_id,
            f"deep researcher error in frontmatter: {research.error}",
            research.frontmatter,
        )
        return None

    if research.market_id is not None and research.market_id != market_id:
        quarantine_market(
            vault,
            market_id,
            f"deep researcher returned mismatched market_id: {research.market_id!r}",
            research.frontmatter,
        )
        return None

    from_edge = bool(row.get("_edge_research_refresh"))
    edge_count = (
        _read_edge_research_refresh_count(vault, market_id) + 1
        if from_edge
        else 0
    )

    payload = {
        "market_id": market_id,
        "estimated_p": research.estimated_p,
        "error": None,
        "edge_research_refresh_count": edge_count,
    }
    if not vault_write_or_quarantine(
        vault=vault,
        market_id=market_id,
        write_fn=lambda: vault.write_research_report(market_id, payload, research.body),
        payload=payload,
        artifact_label="research report",
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
                quarantine_market(vault, market_id, reason, parsed or payload)
                continue

            if PAPER_TRADE_MODE:
                parsed = {**parsed, "executed": False, "transaction_hash": None}

            vault_write_or_quarantine(
                vault=vault,
                market_id=market_id,
                write_fn=lambda: vault.write_trade_log(market_id, parsed),
                payload=parsed,
                artifact_label="trade log",
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
        quarantine_market(vault, market_id, reason, parsed or payload)
        return

    if not vault_write_or_quarantine(
        vault=vault,
        market_id=market_id,
        write_fn=lambda: vault.append_post_mortem(market_id, parsed),
        payload=parsed,
        artifact_label="post-mortem append",
    ):
        return

    # Archive so subsequent ticks don't re-resolve already-analysed markets.
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
        log.error("Overseer failed: %s", reason)
        vault.write_error_log(
            "__overseer__",
            parsed or payload,
            f"overseer rejected: {reason}",
        )
        return

    try:
        vault.write_directives(parsed)
    except VaultWriteError as exc:
        log.error("Directives validation failed: %s", exc.cause)
        vault.write_error_log(
            "__overseer__",
            parsed,
            f"directives validation failed: {exc.cause}",
        )


__all__ = [
    "phase1_data_ingestion",
    "phase2_quantitative_routing",
    "merge_phase3_inputs",
    "phase3_qualitative_pipeline",
    "phase4_execution",
    "phase5_resolution_and_post_mortem",
    "phase6_macro_learning_loop",
]
