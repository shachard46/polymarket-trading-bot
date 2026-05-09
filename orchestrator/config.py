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
]
