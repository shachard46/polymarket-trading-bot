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
# "direct" calls the Anthropic Messages API (no OpenClaw gateway needed).
RUNNER_MODE_DIRECT = "direct"
_VALID_RUNNER_MODES = frozenset(
    {RUNNER_MODE_STUB, RUNNER_MODE_STUB_ERROR, RUNNER_MODE_LIVE, RUNNER_MODE_DIRECT}
)

# Direct backend: model per agent role (env override DIRECT_MODEL_<ROLE upper>).
DIRECT_MODEL_ENV_PREFIX = "DIRECT_MODEL_"
DEFAULT_DIRECT_MODELS = {
    "triage": "claude-haiku-5-5",
    "briefer": "claude-sonnet-5-5",
    "web_searcher": "claude-sonnet-5-5",
    "deep_researcher": "claude-sonnet-5-5",
    "post_mortem_analyst": "claude-sonnet-5-5",
    "overseer": "claude-opus-5-5",
}
FALLBACK_DIRECT_MODEL = "claude-sonnet-5-5"
DIRECT_MAX_TOKENS_ENV = "DIRECT_MAX_TOKENS"
DEFAULT_DIRECT_MAX_TOKENS = 16000
DIRECT_WEB_MAX_USES_ENV = "DIRECT_WEB_MAX_USES"
DEFAULT_DIRECT_WEB_MAX_USES = 8

OPENCLAW_BIN_ENV = "OPENCLAW_BIN"
OPENCLAW_AGENT_TIMEOUT_ENV = "OPENCLAW_AGENT_TIMEOUT"
DEFAULT_OPENCLAW_AGENT_TIMEOUT = 600

OPENCLAW_AGENT_MAX_ATTEMPTS_ENV = "OPENCLAW_AGENT_MAX_ATTEMPTS"
DEFAULT_OPENCLAW_AGENT_MAX_ATTEMPTS = 3

OPENCLAW_AGENT_RETRY_BACKOFF_ENV = "OPENCLAW_AGENT_RETRY_BACKOFF"
DEFAULT_OPENCLAW_AGENT_RETRY_BACKOFF = 2.0

AUTO_REPLAY_ENV = "OPENCLAW_AUTO_REPLAY"

TOP_QUALITATIVE_MARKETS_ENV = "OPENCLAW_TOP_MARKETS"

# Funnel budgets (docs/02): how many markets survive each stage per tick.
GATE_BUDGET_ENV = "OPENCLAW_GATE_BUDGET"
DEFAULT_GATE_BUDGET = 15
TRIAGE_BUDGET_ENV = "OPENCLAW_TRIAGE_BUDGET"
DEFAULT_TRIAGE_BUDGET = 8
QUICK_RESEARCH_BUDGET_ENV = "OPENCLAW_QUICK_RESEARCH_BUDGET"
DEFAULT_QUICK_RESEARCH_BUDGET = 8
FULL_RESEARCH_BUDGET_ENV = "OPENCLAW_FULL_RESEARCH_BUDGET"
DEFAULT_FULL_RESEARCH_BUDGET = 3

# Phase 3 concurrency / wall-clock (FULL stage runs markets in parallel).
RESEARCH_WORKERS_ENV = "OPENCLAW_RESEARCH_WORKERS"
DEFAULT_RESEARCH_WORKERS = 3
RESEARCH_TICK_BUDGET_ENV = "OPENCLAW_RESEARCH_TICK_BUDGET_SEC"
DEFAULT_RESEARCH_TICK_BUDGET_SEC = 1800

# Phase 3 Forensic Pipeline — FULL stage: one round + one follow-up, then forced synthesis.
MAX_FORMAT_RETRIES = 2
MAX_RESEARCH_ITERATIONS = 2
RESEARCH_DATA_MAX_CHARS = 4000

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


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def gate_budget() -> int:
    """Max eligible markets sent from the gate to triage per tick (default 15)."""
    return _int_env(GATE_BUDGET_ENV, DEFAULT_GATE_BUDGET)


def triage_budget() -> int:
    """Max researchable markets promoted to Phase 3 per tick (default 8)."""
    return _int_env(TRIAGE_BUDGET_ENV, DEFAULT_TRIAGE_BUDGET)


def quick_research_budget() -> int:
    """Max markets given a quick research pass per tick (default 8)."""
    return _int_env(QUICK_RESEARCH_BUDGET_ENV, DEFAULT_QUICK_RESEARCH_BUDGET)


def full_research_budget() -> int:
    """Max markets given full research per tick (default 3)."""
    return _int_env(FULL_RESEARCH_BUDGET_ENV, DEFAULT_FULL_RESEARCH_BUDGET)


def research_workers() -> int:
    """Concurrent FULL-stage markets per tick (default 3)."""
    return max(1, _int_env(RESEARCH_WORKERS_ENV, DEFAULT_RESEARCH_WORKERS))


def research_tick_budget_sec() -> int:
    """Wall-clock seconds after which Phase 3 starts no new FULL-stage market (default 1800)."""
    return _int_env(RESEARCH_TICK_BUDGET_ENV, DEFAULT_RESEARCH_TICK_BUDGET_SEC)


# Phase 3 evidence source: A-IQ, a price-blind web searcher agent, or a stable per-market A/B.
EVIDENCE_SOURCE_ENV = "OPENCLAW_EVIDENCE_SOURCE"
EVIDENCE_AIQ = "aiq"
EVIDENCE_WEB = "web"
EVIDENCE_AB = "ab"
_VALID_EVIDENCE_SOURCES = frozenset({EVIDENCE_AIQ, EVIDENCE_WEB, EVIDENCE_AB})
DEFAULT_EVIDENCE_SOURCE = EVIDENCE_AB


def evidence_source() -> str:
    """``aiq`` | ``web`` | ``ab`` (default ``ab``); read at call time. Unknown values raise."""
    mode = os.environ.get(EVIDENCE_SOURCE_ENV, DEFAULT_EVIDENCE_SOURCE).strip().lower()
    if mode not in _VALID_EVIDENCE_SOURCES:
        raise ValueError(
            f"Invalid {EVIDENCE_SOURCE_ENV}={mode!r}; expected one of "
            f"{sorted(_VALID_EVIDENCE_SOURCES)}"
        )
    return mode


OVERSEER_MIN_NEW_RESOLVED_ENV = "OVERSEER_MIN_NEW_RESOLVED"
DEFAULT_OVERSEER_MIN_NEW_RESOLVED = 30


def overseer_min_new_resolved() -> int:
    """New ledger resolution rows required since the last Overseer run (default 30)."""
    return max(0, _int_env(OVERSEER_MIN_NEW_RESOLVED_ENV, DEFAULT_OVERSEER_MIN_NEW_RESOLVED))


def runner_mode() -> str:
    """Return the active runner mode, defaulting to ``stub``.

    Raises :class:`ValueError` on an unknown value so a typo can never silently
    fall back to stub mode.
    """
    mode = os.environ.get(RUNNER_MODE_ENV, RUNNER_MODE_STUB).strip().lower()
    if mode not in _VALID_RUNNER_MODES:
        raise ValueError(
            f"Invalid {RUNNER_MODE_ENV}={mode!r}; expected one of "
            f"{sorted(_VALID_RUNNER_MODES)}"
        )
    return mode


def direct_model(role: str) -> str:
    """Model id for a role in direct mode: ``DIRECT_MODEL_<ROLE>`` env, else the role default."""
    override = os.environ.get(DIRECT_MODEL_ENV_PREFIX + role.upper(), "").strip()
    if override:
        return override
    return DEFAULT_DIRECT_MODELS.get(role, FALLBACK_DIRECT_MODEL)


def direct_max_tokens() -> int:
    """Max output tokens per direct call (default 16000)."""
    return max(1, _int_env(DIRECT_MAX_TOKENS_ENV, DEFAULT_DIRECT_MAX_TOKENS))


def direct_web_max_uses() -> int:
    """Cap on server-side web_search/web_fetch uses per web_searcher call (default 8)."""
    return max(1, _int_env(DIRECT_WEB_MAX_USES_ENV, DEFAULT_DIRECT_WEB_MAX_USES))


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
    raw = os.environ.get(AUTO_REPLAY_ENV, "")
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
    "RUNNER_MODE_DIRECT",
    "direct_model",
    "direct_max_tokens",
    "direct_web_max_uses",
    "runner_mode",
    "OPENCLAW_BIN_ENV",
    "OPENCLAW_AGENT_TIMEOUT_ENV",
    "OPENCLAW_AGENT_MAX_ATTEMPTS_ENV",
    "OPENCLAW_AGENT_RETRY_BACKOFF_ENV",
    "AUTO_REPLAY_ENV",
    "auto_replay",
    "openclaw_bin",
    "openclaw_agent_timeout",
    "openclaw_agent_max_attempts",
    "openclaw_agent_retry_backoff",
    "TOP_QUALITATIVE_MARKETS_ENV",
    "top_qualitative_markets",
    "GATE_BUDGET_ENV",
    "TRIAGE_BUDGET_ENV",
    "QUICK_RESEARCH_BUDGET_ENV",
    "FULL_RESEARCH_BUDGET_ENV",
    "gate_budget",
    "triage_budget",
    "quick_research_budget",
    "full_research_budget",
    "OVERSEER_MIN_NEW_RESOLVED_ENV",
    "overseer_min_new_resolved",
    "RESEARCH_WORKERS_ENV",
    "RESEARCH_TICK_BUDGET_ENV",
    "research_workers",
    "research_tick_budget_sec",
    "MAX_FORMAT_RETRIES",
    "MAX_RESEARCH_ITERATIONS",
    "RESEARCH_DATA_MAX_CHARS",
    "FORCED_SYNTHESIS_OVERRIDE",
    "VAULT_PATH_ENV",
    "resolve_vault_base",
]
