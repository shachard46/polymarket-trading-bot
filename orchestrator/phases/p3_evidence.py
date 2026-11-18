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
    return bool(str(entry.get("research_data") or "").strip()) and not entry.get("error")


def _bundle_has_entries(bundle: list[dict[str, Any]] | None) -> bool:
    """True only when at least one row carries data and no error (not a poisoned bundle)."""
    return any(_bundle_row_usable(e) for e in bundle or [])


def _ensure_as_of(query: str, today: str) -> str:
    return query if f"as of {today}" in query.lower() else f"{query.rstrip()} (as of {today})"



def _aiq_cache_path(vault: ObsidianManager) -> Path:
    return vault._dirs["active"] / "aiq_cache.json"


def fetch_other_source_bundle(
    vault: ObsidianManager,
    market_id: str,
    queries: list[str],
    used_source: str,
    runner: AgentRunner = spawn_agent,
    today: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Fetch ``queries`` from the evidence source OTHER than ``used_source`` (aiq <-> web).

    Returns ``(source, rows)``; nothing is persisted. Web rows come sanitized (price-blind).
    """
    cleaned = [str(q).strip() for q in queries if str(q).strip()]
    if used_source == evidence.AIQ:
        return evidence.WEB, evidence.fetch_web_bundle(
            runner, vault, market_id, cleaned, today or today_date()
        )
    rows = fetch_research_bundle_cached(
        cleaned, _aiq_cache_path(vault), fetch=fetch_research_bundle
    )
    return evidence.AIQ, rows


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
    source = stored.get("evidence_source") or evidence.intended_source(market_id)
    fallback = bool(stored.get("evidence_fallback"))
    when = today or today_date()

    def web() -> list[dict[str, Any]]:
        return evidence.fetch_web_bundle(runner, vault, market_id, cleaned, when)

    if source == evidence.AIQ:
        results = fetch_research_bundle_cached(
            cleaned, _aiq_cache_path(vault), fetch=fetch_research_bundle
        )
        if evidence_source() == EVIDENCE_AB and not evidence.rows_usable(results):
            log.warning("[PHASE 3] A-IQ arm failed for %s; falling back to web search", market_id)
            results, source, fallback = web(), evidence.WEB, True
    else:
        results = web()
    meta = {"evidence_source": source, "evidence_fallback": fallback}
    vault.write_research_bundle(market_id, results, meta=meta)
    return meta
