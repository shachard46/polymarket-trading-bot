"""Phase 3 evidence: fetch the A-IQ / web-search bundle for a market and persist it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from obsidian_utils import ObsidianManager
from orchestrator import evidence
from orchestrator.aiq_bundle import fetch_research_bundle, fetch_research_bundle_cached
from orchestrator.config import EVIDENCE_AB, evidence_source
from orchestrator.live_prompt import today_date
from orchestrator.phases.common import log
from orchestrator.runner import AgentRunner, spawn_agent


def _bundle_row_usable(entry: dict[str, Any]) -> bool:
    return evidence.row_usable(entry)


def _bundle_has_entries(bundle: list[dict[str, Any]] | None) -> bool:
    """True only when at least one row carries data and no error (not a poisoned bundle)."""
    return any(_bundle_row_usable(e) for e in bundle or [])


def _ensure_as_of(query: str, today: str) -> str:
    return query if f"as of {today}" in query.lower() else f"{query.rstrip()} (as of {today})"



def _aiq_cache_path(vault: ObsidianManager) -> Path:
    return vault._dirs["active"] / "aiq_cache.json"


def resolve_source(vault: ObsidianManager, market_id: str) -> tuple[str, bool]:
    """The market's evidence ``(source, fallback)``: the recorded one, else chosen by ``PMBOT_EVIDENCE_SOURCE``."""
    stored = vault.read_research_bundle_meta(market_id)
    return (
        stored.get("evidence_source") or evidence.intended_source(market_id),
        bool(stored.get("evidence_fallback")),
    )


def fetch_rows(
    vault: ObsidianManager,
    market_id: str,
    queries: list[str],
    runner: AgentRunner,
    today: str,
    source: str,
) -> tuple[list[dict[str, Any]], str, bool]:
    """Evidence rows for ``queries`` from ``source``; returns ``(rows, source_used, fell_back)``.

    Pure with respect to the vault (nothing is written), so it is safe to call from worker threads. In ``ab``
    mode an A-IQ arm whose rows all failed falls back to web.
    """
    cleaned = [str(q).strip() for q in queries if str(q).strip()]
    if not cleaned:
        return [], source, False
    if source == evidence.AIQ:
        rows = fetch_research_bundle_cached(
            cleaned, _aiq_cache_path(vault), fetch=fetch_research_bundle
        )
        if evidence_source() == EVIDENCE_AB and not evidence.rows_usable(rows):
            log.warning("[PHASE 3] A-IQ arm failed for %s; falling back to web search", market_id)
            return evidence.fetch_web_bundle(runner, vault, market_id, cleaned, today), evidence.WEB, True
        return rows, source, False
    return evidence.fetch_web_bundle(runner, vault, market_id, cleaned, today), source, False


def _fetch_and_persist_bundle(
    vault: ObsidianManager,
    market_id: str,
    queries: list[str],
    runner: AgentRunner = spawn_agent,
    today: str | None = None,
) -> dict[str, Any]:
    """Fetch evidence for ``queries`` (A-IQ or web searcher) and persist the bundle.

    The source is the market's recorded one (follow-up queries stay on it), else chosen by
    ``PMBOT_EVIDENCE_SOURCE`` (``ab``: stable per-market arm). In ``ab`` mode an A-IQ arm
    whose rows all failed falls back to web. Returns ``{evidence_source, evidence_fallback}``.
    """
    cleaned = [str(q).strip() for q in queries if str(q).strip()]
    stored = vault.read_research_bundle_meta(market_id)
    if not cleaned:
        return stored
    source, fallback = resolve_source(vault, market_id)
    results, source, fell = fetch_rows(vault, market_id, cleaned, runner, today or today_date(), source)
    meta = {"evidence_source": source, "evidence_fallback": fallback or fell}
    vault.write_research_bundle(market_id, results, meta=meta)
    return meta
