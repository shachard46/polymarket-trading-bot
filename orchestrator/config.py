"""Orchestrator-level configuration: env flags + re-exports of trading constants."""

from __future__ import annotations

import os

from config.trading_constants import (
    FILTERS,
    OVERSEER_INTERVAL_SEC,
    PAPER_TRADE_MODE,
    PIPELINE_INTERVAL_SEC,
)

# Execution mode for the OpenClaw runner. "stub" prints + returns empty so the
# DLQ path is exercised in CI without a live gateway; "live" calls the SDK.
RUNNER_MODE_ENV = "OPENCLAW_ORCHESTRATOR_MODE"
RUNNER_MODE_STUB = "stub"
RUNNER_MODE_STUB_ERROR = "stub_error"
RUNNER_MODE_LIVE = "live"

TOP_QUALITATIVE_MARKETS_ENV = "OPENCLAW_TOP_MARKETS"

# Max completed edge-triggered Deep Researcher refresh cycles per market (initial research is not counted).
EDGE_RESEARCH_REFRESH_MAX_ENV = "OPENCLAW_EDGE_RESEARCH_REFRESH_MAX"
DEFAULT_EDGE_RESEARCH_REFRESH_MAX = 3


def top_qualitative_markets() -> int:
    """Max markets promoted from quantitative gate to qualitative pipeline (default 20)."""
    raw = os.environ.get(TOP_QUALITATIVE_MARKETS_ENV, "20")
    try:
        return max(1, int(raw))
    except ValueError:
        return 20


def max_edge_research_refreshes() -> int:
    """Cap edge-disqualification research refresh cycles per market (default 3)."""
    raw = os.environ.get(EDGE_RESEARCH_REFRESH_MAX_ENV)
    if not raw:
        return DEFAULT_EDGE_RESEARCH_REFRESH_MAX
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_EDGE_RESEARCH_REFRESH_MAX


def runner_mode() -> str:
    """Return the active runner mode, defaulting to ``stub``."""
    return os.environ.get(RUNNER_MODE_ENV, RUNNER_MODE_STUB).strip().lower()


__all__ = [
    "FILTERS",
    "OVERSEER_INTERVAL_SEC",
    "PAPER_TRADE_MODE",
    "PIPELINE_INTERVAL_SEC",
    "RUNNER_MODE_ENV",
    "RUNNER_MODE_STUB",
    "RUNNER_MODE_STUB_ERROR",
    "RUNNER_MODE_LIVE",
    "runner_mode",
    "TOP_QUALITATIVE_MARKETS_ENV",
    "top_qualitative_markets",
    "EDGE_RESEARCH_REFRESH_MAX_ENV",
    "max_edge_research_refreshes",
]
