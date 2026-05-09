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
from pathlib import Path
from typing import Any

from obsidian_utils import ObsidianManager, VaultWriteError
from orchestrator import scraper
from orchestrator.config import PAPER_TRADE_MODE
from orchestrator.dead_letter import quarantine_market
from orchestrator.parse import (
    AgentOutputParseError,
    agent_error_reason,
    coerce_deep_researcher_markdown,
    parse_agent_json_or_yaml,
)
from orchestrator.research import parse_deep_researcher
from orchestrator.runner import AgentRunner, spawn_agent

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _active_research_path(vault: ObsidianManager, market_id: str) -> Path:
    return vault._dirs["active"] / f"{market_id}.md"  # noqa: SLF001


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


def phase1_data_ingestion(vault: ObsidianManager) -> list[str]:
    """Query polymarket-scraper for high-delta / new markets."""
    log.info("[PHASE 1] Data ingestion")
    target_market_ids = scraper.fetch_target_market_ids()
    log.info("[PHASE 1] target_market_ids=%r", target_market_ids)
    return target_market_ids


# ---------------------------------------------------------------------------
# Phase 2 — Quantitative routing
# ---------------------------------------------------------------------------


def phase2_quantitative_routing(
    vault: ObsidianManager,
    target_market_ids: list[str],
    runner: AgentRunner = spawn_agent,
) -> list[dict[str, Any]]:
    """Filter markets through the (re-)Evaluator's quantitative skill."""
    log.info("[PHASE 2] Quantitative routing")
    limit = scraper.trends_limit_for_filters()
    passed: list[dict[str, Any]] = []

    for market_id in target_market_ids:
        try:
            historic = list(reversed(scraper.get_market_trends(market_id, limit)))
            role = (
                "re_evaluator"
                if _active_research_path(vault, market_id).exists()
                else "evaluator"
            )
            payload = {"market_id": market_id, "historic_market_data": historic}

            parsed, reason = _run_structured_agent(runner, role, payload)
            if reason:
                quarantine_market(vault, market_id, reason, parsed or payload)
                continue

            try:
                vault.write_filter_log(market_id, parsed)
            except VaultWriteError as exc:
                quarantine_market(
                    vault,
                    market_id,
                    f"filter log validation failed: {exc.cause}",
                    parsed,
                )
                continue

            if not parsed.get("passed"):
                log.info("Market %s did not pass quantitative filters", market_id)
                continue

            passed.append(
                {
                    "market_id": market_id,
                    "market_title": f"Market {market_id}",
                    "market_description": "",
                    "market_data": {},
                    "evaluator_output": parsed,
                }
            )
        except Exception as exc:  # noqa: BLE001 — pipeline continues per market
            quarantine_market(
                vault, market_id, f"phase2 exception: {exc!r}", {"exception": repr(exc)}
            )

    log.info("[PHASE 2] passed_markets count=%d", len(passed))
    return passed


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
        try:
            researched_row = _research_market(vault, runner, row, directives)
        except Exception as exc:  # noqa: BLE001
            quarantine_market(
                vault, market_id, f"phase3 exception: {exc!r}", {"exception": repr(exc)}
            )
            continue
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

    payload = {
        "market_id": research.market_id or market_id,
        "estimated_p": research.estimated_p,
        "error": None,
    }
    try:
        vault.write_research_report(market_id, payload, research.body)
    except VaultWriteError as exc:
        quarantine_market(
            vault,
            market_id,
            f"research report validation failed: {exc.cause}",
            payload,
        )
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
        try:
            payload = {
                "market_id": market_id,
                "p_value": row["p_value"],
                "market_data": row.get("market_data") or {},
            }
            parsed, reason = _run_structured_agent(runner, "executioner", payload)
            if reason:
                quarantine_market(vault, market_id, reason, parsed or payload)
                continue

            if PAPER_TRADE_MODE:
                parsed = {**parsed, "executed": False, "transaction_hash": None}

            try:
                vault.write_trade_log(market_id, parsed)
            except VaultWriteError as exc:
                quarantine_market(
                    vault,
                    market_id,
                    f"trade log validation failed: {exc.cause}",
                    parsed,
                )
        except Exception as exc:  # noqa: BLE001
            quarantine_market(
                vault, market_id, f"phase4 exception: {exc!r}", {"exception": repr(exc)}
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
    trades_dir: Path = vault._dirs["trades"]  # noqa: SLF001
    for trade_path in sorted(trades_dir.glob("*.json")):
        market_id = trade_path.stem
        resolution = scraper.fetch_resolution(market_id)
        if resolution is None:
            continue

        try:
            vault.move_file(market_id, "active", "post_mortem")
        except FileNotFoundError:
            log.warning("No active research file to move for %s", market_id)
            continue

        post_md = vault._dirs["post_mortem"] / f"{market_id}.md"  # noqa: SLF001
        payload = {
            "market_id": market_id,
            "original_research": post_md.read_text(encoding="utf-8"),
            "execution_log": trade_path.read_text(encoding="utf-8"),
            "resolution_data": resolution,
        }
        parsed, reason = _run_structured_agent(runner, "post_mortem_analyst", payload)
        if reason:
            quarantine_market(vault, market_id, reason, parsed or payload)
            continue

        try:
            vault.append_post_mortem(market_id, parsed)
        except VaultWriteError as exc:
            quarantine_market(
                vault,
                market_id,
                f"post-mortem append validation failed: {exc.cause}",
                parsed,
            )


# ---------------------------------------------------------------------------
# Phase 6 — Macro-learning (Overseer)
# ---------------------------------------------------------------------------


def phase6_macro_learning_loop(
    vault: ObsidianManager,
    runner: AgentRunner = spawn_agent,
) -> None:
    """Aggregate post-mortems → Overseer → overwrite ``active_directives.md``."""
    log.info("[PHASE 6] Macro-learning loop (Overseer)")
    post_dir: Path = vault._dirs["post_mortem"]  # noqa: SLF001
    batch = [
        {"market_id": p.stem, "content": p.read_text(encoding="utf-8")}
        for p in sorted(post_dir.glob("*.md"))
    ]
    payload = {
        "post_mortems": batch,
        "current_directives": vault.read_directives(),
    }
    parsed, reason = _run_structured_agent(runner, "overseer", payload)
    if reason:
        log.error("Overseer failed: %s", reason)
        return

    try:
        vault.write_directives(parsed)
    except VaultWriteError as exc:
        log.error("Directives validation failed: %s", exc.cause)


__all__ = [
    "phase1_data_ingestion",
    "phase2_quantitative_routing",
    "phase3_qualitative_pipeline",
    "phase4_execution",
    "phase5_resolution_and_post_mortem",
    "phase6_macro_learning_loop",
]
