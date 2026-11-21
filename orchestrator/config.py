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

# Execution mode of the agent runner. "stub" returns deterministic responses for CI; "live" calls
# the LLM providers directly (provider + model per role: config/models.yaml, see orchestrator/llm).
RUNNER_MODE_ENV = "PMBOT_MODE"
RUNNER_MODE_STUB = "stub"
RUNNER_MODE_STUB_ERROR = "stub_error"
RUNNER_MODE_LIVE = "live"
_VALID_RUNNER_MODES = frozenset({RUNNER_MODE_STUB, RUNNER_MODE_STUB_ERROR, RUNNER_MODE_LIVE})

OPENROUTER_BASE_URL_ENV = "OPENROUTER_BASE_URL"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Defaults for models.yaml (`max_tokens`, `web_max_uses`).
DEFAULT_DIRECT_MAX_TOKENS = 16000
DEFAULT_DIRECT_WEB_MAX_USES = 8

PMBOT_LLM_TIMEOUT_ENV = "PMBOT_LLM_TIMEOUT"
DEFAULT_PMBOT_LLM_TIMEOUT = 600

PMBOT_LLM_MAX_ATTEMPTS_ENV = "PMBOT_LLM_MAX_ATTEMPTS"
DEFAULT_PMBOT_LLM_MAX_ATTEMPTS = 3

PMBOT_LLM_RETRY_BACKOFF_ENV = "PMBOT_LLM_RETRY_BACKOFF"
DEFAULT_PMBOT_LLM_RETRY_BACKOFF = 2.0

# Batch API (models.yaml role `batch: true`): poll interval and the cap after which the batch is
# cancelled and the unfinished items run synchronously.
PMBOT_LLM_BATCH_POLL_SEC_ENV = "PMBOT_LLM_BATCH_POLL_SEC"
DEFAULT_PMBOT_LLM_BATCH_POLL_SEC = 30.0
PMBOT_LLM_BATCH_MAX_WAIT_SEC_ENV = "PMBOT_LLM_BATCH_MAX_WAIT_SEC"
DEFAULT_PMBOT_LLM_BATCH_MAX_WAIT_SEC = 1200.0

AUTO_REPLAY_ENV = "PMBOT_AUTO_REPLAY"

TOP_QUALITATIVE_MARKETS_ENV = "PMBOT_TOP_MARKETS"

# Funnel budgets (docs/02): how many markets survive each stage per tick.
GATE_BUDGET_ENV = "PMBOT_GATE_BUDGET"
DEFAULT_GATE_BUDGET = 0  # 0 = unlimited (every eligible market is triaged)
TRIAGE_BUDGET_ENV = "PMBOT_TRIAGE_BUDGET"
DEFAULT_TRIAGE_BUDGET = 0  # 0 = unlimited (every researchable market is promoted)
QUICK_RESEARCH_BUDGET_ENV = "PMBOT_QUICK_RESEARCH_BUDGET"
DEFAULT_QUICK_RESEARCH_BUDGET = 8
FULL_RESEARCH_BUDGET_ENV = "PMBOT_FULL_RESEARCH_BUDGET"
DEFAULT_FULL_RESEARCH_BUDGET = 3

# Phase 3 concurrency / wall-clock (FULL stage runs markets in parallel).
RESEARCH_WORKERS_ENV = "PMBOT_RESEARCH_WORKERS"
DEFAULT_RESEARCH_WORKERS = 3
RESEARCH_TICK_BUDGET_ENV = "PMBOT_RESEARCH_TICK_BUDGET_SEC"
DEFAULT_RESEARCH_TICK_BUDGET_SEC = 1800

# Phase 3 Forensic Pipeline — FULL stage: the judges are forced to synthesize (no needs_more rounds).
MAX_FORMAT_RETRIES = 2
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
    """Max eligible markets sent from the gate to triage per tick (default 0 = unlimited; >0 caps)."""
    return _int_env(GATE_BUDGET_ENV, DEFAULT_GATE_BUDGET)


def triage_budget() -> int:
    """Max researchable markets promoted to Phase 3 per tick (default 0 = unlimited; >0 caps)."""
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
EVIDENCE_SOURCE_ENV = "PMBOT_EVIDENCE_SOURCE"
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


# Judge arm: 1 judge (framing YES or NO, picked per market by hash, flipped back in code) vs 2 judges (J1 YES + J2 NO).
JUDGE_ARM_MODE_ENV = "JUDGE_ARM_MODE"
JUDGE_ARM_ONE = "one"
JUDGE_ARM_TWO = "two"
JUDGE_ARM_AB = "ab"
DEFAULT_JUDGE_ARM_MODE = JUDGE_ARM_AB


def judge_arm_mode() -> str:
    """``ab`` | ``one`` | ``two`` (default ``ab``: stable per-market arm); read at call time. Unknown values raise."""
    mode = os.environ.get(JUDGE_ARM_MODE_ENV, DEFAULT_JUDGE_ARM_MODE).strip().lower() or DEFAULT_JUDGE_ARM_MODE
    if mode not in (JUDGE_ARM_AB, JUDGE_ARM_ONE, JUDGE_ARM_TWO):
        raise ValueError(f"Invalid {JUDGE_ARM_MODE_ENV}={mode!r}; expected one of ['ab', 'one', 'two']")
    return mode


# Judge model A/B: share of markets whose deep_researcher calls use an alternative model (see evidence.judge_model_arm).
JUDGE_MODEL_AB_ENV = "PMBOT_JUDGE_MODEL_AB"
JUDGE_MODEL_ALT_ENV = "PMBOT_JUDGE_MODEL_ALT"
JUDGE_MODEL_ALT_SHARE_ENV = "PMBOT_JUDGE_MODEL_ALT_SHARE"


def judge_model_ab_on() -> bool:
    """``on`` (default) | ``off`` (every market uses the models.yaml judge); read at call time. Unknown values raise."""
    from config.trading_constants import JUDGE_MODEL_AB_DEFAULT  # noqa: PLC0415

    raw = os.environ.get(JUDGE_MODEL_AB_ENV, JUDGE_MODEL_AB_DEFAULT).strip().lower() or JUDGE_MODEL_AB_DEFAULT
    if raw not in ("on", "off"):
        raise ValueError(f"Invalid {JUDGE_MODEL_AB_ENV}={raw!r}; expected 'on' or 'off'")
    return raw == "on"


def judge_model_alt() -> str:
    """``provider:model`` of the alternative judge (default openrouter:deepseek/deepseek-v4.1-flash)."""
    from config.trading_constants import JUDGE_MODEL_ALT_DEFAULT  # noqa: PLC0415

    return os.environ.get(JUDGE_MODEL_ALT_ENV, "").strip() or JUDGE_MODEL_ALT_DEFAULT


def judge_model_alt_share() -> float:
    """Share of markets (0..1) judged by the alternative model (default 0.6). Out-of-range / non-numeric raises."""
    from config.trading_constants import JUDGE_MODEL_ALT_SHARE_DEFAULT  # noqa: PLC0415

    raw = os.environ.get(JUDGE_MODEL_ALT_SHARE_ENV, "").strip()
    try:
        share = float(raw) if raw else JUDGE_MODEL_ALT_SHARE_DEFAULT
    except ValueError:
        raise ValueError(f"Invalid {JUDGE_MODEL_ALT_SHARE_ENV}={raw!r}; expected a number in [0, 1]") from None
    if not 0.0 <= share <= 1.0:
        raise ValueError(f"Invalid {JUDGE_MODEL_ALT_SHARE_ENV}={raw!r}; expected a number in [0, 1]")
    return share


# Web search backend behind the "web" evidence arm: Brave Search + page fetch + extractor, or the gemini
# web_searcher agent. Default: brave when BRAVE_API_KEY is set (live mode only), else gemini.
WEB_BACKEND_ENV = "PMBOT_WEB_BACKEND"
BRAVE_API_KEY_ENV = "BRAVE_API_KEY"
WEB_BACKEND_BRAVE = "brave"
WEB_BACKEND_GEMINI = "gemini"


def web_backend() -> str:
    """``brave`` | ``gemini``; read at call time. Unknown values raise.

    Brave needs ``BRAVE_API_KEY`` and live runner mode; without them (or with ``PMBOT_WEB_BACKEND=gemini``)
    the answer is gemini, so stub runs and tests never touch the network.
    """
    mode = os.environ.get(WEB_BACKEND_ENV, "").strip().lower()
    if mode not in ("", WEB_BACKEND_BRAVE, WEB_BACKEND_GEMINI):
        raise ValueError(
            f"Invalid {WEB_BACKEND_ENV}={mode!r}; expected one of ['brave', 'gemini']"
        )
    if mode == WEB_BACKEND_GEMINI:
        return WEB_BACKEND_GEMINI
    if not os.environ.get(BRAVE_API_KEY_ENV, "").strip() or runner_mode() != RUNNER_MODE_LIVE:
        return WEB_BACKEND_GEMINI
    return WEB_BACKEND_BRAVE


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


def openrouter_base_url() -> str:
    return (os.environ.get(OPENROUTER_BASE_URL_ENV, "").strip() or DEFAULT_OPENROUTER_BASE_URL).rstrip("/")


def llm_timeout() -> int:
    """Return per-call LLM HTTP timeout in seconds."""
    raw = os.environ.get(PMBOT_LLM_TIMEOUT_ENV)
    if not raw:
        return DEFAULT_PMBOT_LLM_TIMEOUT
    try:
        return max(10, int(raw))
    except ValueError:
        return DEFAULT_PMBOT_LLM_TIMEOUT


def llm_max_attempts() -> int:
    """Return max LLM call attempts before surfacing failure."""
    raw = os.environ.get(PMBOT_LLM_MAX_ATTEMPTS_ENV)
    if not raw:
        return DEFAULT_PMBOT_LLM_MAX_ATTEMPTS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_PMBOT_LLM_MAX_ATTEMPTS


def llm_retry_backoff() -> float:
    """Return base seconds for exponential backoff between LLM call retries."""
    raw = os.environ.get(PMBOT_LLM_RETRY_BACKOFF_ENV)
    if not raw:
        return DEFAULT_PMBOT_LLM_RETRY_BACKOFF
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_PMBOT_LLM_RETRY_BACKOFF


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def llm_batch_poll_sec() -> float:
    """Seconds between batch status polls (default 30)."""
    return _float_env(PMBOT_LLM_BATCH_POLL_SEC_ENV, DEFAULT_PMBOT_LLM_BATCH_POLL_SEC)


def llm_batch_max_wait_sec() -> float:
    """Max seconds to wait for a batch before cancelling and going synchronous (default 1200)."""
    return _float_env(PMBOT_LLM_BATCH_MAX_WAIT_SEC_ENV, DEFAULT_PMBOT_LLM_BATCH_MAX_WAIT_SEC)


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
    "openrouter_base_url",
    "runner_mode",
    "PMBOT_LLM_TIMEOUT_ENV",
    "PMBOT_LLM_MAX_ATTEMPTS_ENV",
    "PMBOT_LLM_RETRY_BACKOFF_ENV",
    "AUTO_REPLAY_ENV",
    "auto_replay",
    "llm_timeout",
    "llm_max_attempts",
    "llm_retry_backoff",
    "PMBOT_LLM_BATCH_POLL_SEC_ENV",
    "PMBOT_LLM_BATCH_MAX_WAIT_SEC_ENV",
    "llm_batch_poll_sec",
    "llm_batch_max_wait_sec",
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
    "WEB_BACKEND_ENV",
    "BRAVE_API_KEY_ENV",
    "web_backend",
    "RESEARCH_WORKERS_ENV",
    "RESEARCH_TICK_BUDGET_ENV",
    "research_workers",
    "research_tick_budget_sec",
    "MAX_FORMAT_RETRIES",
    "RESEARCH_DATA_MAX_CHARS",
    "FORCED_SYNTHESIS_OVERRIDE",
    "VAULT_PATH_ENV",
    "resolve_vault_base",
]
