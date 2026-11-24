"""Phase 1 - Data ingestion."""

from __future__ import annotations

from obsidian_utils import ObsidianManager
from orchestrator import scraper
from orchestrator.directives import extract_filter_directives
from orchestrator.phases.common import log
from orchestrator.scraper import MarketRow


def phase1_data_ingestion(vault: ObsidianManager) -> list[MarketRow]:
    """Query Polymarket (Gamma) for open binary candidates inside the days window."""
    log.info("[PHASE 1] Data ingestion")
    target_markets = scraper.fetch_target_markets(
        knobs=extract_filter_directives(vault.read_directives()), vault=vault
    )
    log.info(
        "[PHASE 1] target_markets count=%d ids=%r",
        len(target_markets),
        [m.market_id for m in target_markets],
    )
    return target_markets
