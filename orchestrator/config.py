"""Orchestrator-level configuration: env flags + re-exports of trading constants."""

from __future__ import annotations

import os

from config.trading_constants import (
    FILTERS,
    OVERSEER_INTERVAL_SEC,
    PAPER_TRADE_MODE,
    PIPELINE_INTERVAL_SEC,
)
from config.vault import VAULT_PATH_ENV, resolve_vault_base

# Execution mode for the OpenClaw runner. "stub" returns deterministic responses
# for CI; "live" invokes the local OpenClaw Gateway via the OpenClaw CLI.
RUNNER_MODE_ENV = "OPENCLAW_ORCHESTRATOR_MODE"
RUNNER_MODE_STUB = "stub"
RUNNER_MODE_STUB_ERROR = "stub_error"
RUNNER_MODE_LIVE = "live"

OPENCLAW_BIN_ENV = "OPENCLAW_BIN"
OPENCLAW_AGENT_TIMEOUT_ENV = "OPENCLAW_AGENT_TIMEOUT"
DEFAULT_OPENCLAW_AGENT_TIMEOUT = 600

OPENCLAW_AGENT_MAX_ATTEMPTS_ENV = "OPENCLAW_AGENT_MAX_ATTEMPTS"
DEFAULT_OPENCLAW_AGENT_MAX_ATTEMPTS = 3

OPENCLAW_AGENT_RETRY_BACKOFF_ENV = "OPENCLAW_AGENT_RETRY_BACKOFF"
DEFAULT_OPENCLAW_AGENT_RETRY_BACKOFF = 2.0

AUTO_REPLAY_ENV = "OPENCLAW_AUTO_REPLAY"
AUTO_REPLAY_DLQ_ENV = "OPENCLAW_AUTO_REPLAY_DLQ"  # deprecated alias

TOP_QUALITATIVE_MARKETS_ENV = "OPENCLAW_TOP_MARKETS"

# Max completed edge-triggered Deep Researcher refresh cycles per market (initial research is not counted).
EDGE_RESEARCH_REFRESH_MAX_ENV = "OPENCLAW_EDGE_RESEARCH_REFRESH_MAX"
DEFAULT_EDGE_RESEARCH_REFRESH_MAX = 3

# Phase 3 iterative research loop
MAX_FORMAT_RETRIES = 2
MAX_RESEARCH_ITERATIONS = 2

FORCED_SYNTHESIS_OVERRIDE = (
    "SYSTEM OVERRIDE: Maximum research depth reached. You are strictly forbidden "
    "from requesting more data. You must evaluate the current research_bundle and "
    "output a status: complete JSON payload with a final Markdown report and "
    "estimated_p."
)


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


def openclaw_bin() -> str:
    """Return OpenClaw CLI binary name/path."""
    return os.environ.get(OPENCLAW_BIN_ENV, "openclaw").strip() or "openclaw"


def openclaw_agent_timeout() -> int:
    """Return per-agent OpenClaw CLI timeout in seconds."""
    raw = os.environ.get(OPENCLAW_AGENT_TIMEOUT_ENV)
    if not raw:
        return DEFAULT_OPENCLAW_AGENT_TIMEOUT
    try:
        return max(10, int(raw))
    except ValueError:
        return DEFAULT_OPENCLAW_AGENT_TIMEOUT


def openclaw_agent_max_attempts() -> int:
    """Return max OpenClaw agent CLI attempts before surfacing failure."""
    raw = os.environ.get(OPENCLAW_AGENT_MAX_ATTEMPTS_ENV)
    if not raw:
        return DEFAULT_OPENCLAW_AGENT_MAX_ATTEMPTS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_OPENCLAW_AGENT_MAX_ATTEMPTS


def openclaw_agent_retry_backoff() -> float:
    """Return base seconds for exponential backoff between CLI retries."""
    raw = os.environ.get(OPENCLAW_AGENT_RETRY_BACKOFF_ENV)
    if not raw:
        return DEFAULT_OPENCLAW_AGENT_RETRY_BACKOFF
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_OPENCLAW_AGENT_RETRY_BACKOFF


def auto_replay() -> bool:
    """Return whether the orchestrator should clear inactive flags at startup."""
    raw = os.environ.get(AUTO_REPLAY_ENV) or os.environ.get(AUTO_REPLAY_DLQ_ENV, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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
    "OPENCLAW_BIN_ENV",
    "OPENCLAW_AGENT_TIMEOUT_ENV",
    "OPENCLAW_AGENT_MAX_ATTEMPTS_ENV",
    "OPENCLAW_AGENT_RETRY_BACKOFF_ENV",
    "AUTO_REPLAY_ENV",
    "AUTO_REPLAY_DLQ_ENV",
    "auto_replay",
    "openclaw_bin",
    "openclaw_agent_timeout",
    "openclaw_agent_max_attempts",
    "openclaw_agent_retry_backoff",
    "TOP_QUALITATIVE_MARKETS_ENV",
    "top_qualitative_markets",
    "EDGE_RESEARCH_REFRESH_MAX_ENV",
    "max_edge_research_refreshes",
    "MAX_FORMAT_RETRIES",
    "MAX_RESEARCH_ITERATIONS",
    "FORCED_SYNTHESIS_OVERRIDE",
    "VAULT_PATH_ENV",
    "resolve_vault_base",
]
