"""polymarket-scraper integration.

These functions encapsulate ``poly-scan`` CLI calls so phase code stays
free of subprocess details. Failures (binary missing, non-zero exit, bad
JSON) are logged and converted to empty results — the pipeline must keep
running even when the scraper is unavailable.

Environment variables:

- ``POLY_SCAN_BIN``         — path to the ``poly-scan`` executable (default ``poly-scan``).
- ``OPENCLAW_INGEST_LIMIT`` — max markets pulled per ingestion cycle (default ``50``).
- ``POLY_SCAN_TIMEOUT_SEC`` — per-command timeout in seconds (default ``60``).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any

from orchestrator.config import FILTERS

log = logging.getLogger(__name__)

_DEFAULT_BIN = "poly-scan"
_DEFAULT_TIMEOUT_SEC = 60.0
_DEFAULT_INGEST_LIMIT = 50

# Status strings emitted by the scraper that indicate the market has resolved.
_CLOSED_STATUSES = frozenset({"closed", "resolved", "archived"})

# Cache the "binary missing" warning so we don't spam the log each tick.
_warned_missing_binary = False


def _poly_scan_bin() -> str:
    return os.environ.get("POLY_SCAN_BIN", _DEFAULT_BIN)


def _timeout_sec() -> float:
    raw = os.environ.get("POLY_SCAN_TIMEOUT_SEC")
    if not raw:
        return _DEFAULT_TIMEOUT_SEC
    try:
        return float(raw)
    except ValueError:
        log.warning("Invalid POLY_SCAN_TIMEOUT_SEC=%r; using default", raw)
        return _DEFAULT_TIMEOUT_SEC


def _ingest_limit() -> int:
    raw = os.environ.get("OPENCLAW_INGEST_LIMIT")
    if not raw:
        return _DEFAULT_INGEST_LIMIT
    try:
        return max(1, int(raw))
    except ValueError:
        log.warning("Invalid OPENCLAW_INGEST_LIMIT=%r; using default", raw)
        return _DEFAULT_INGEST_LIMIT


def _run_poly_scan(*args: str) -> Any | None:
    """Run ``poly-scan <args>``, return parsed JSON, or ``None`` on any failure."""
    global _warned_missing_binary

    binary = _poly_scan_bin()
    if shutil.which(binary) is None:
        if not _warned_missing_binary:
            log.warning(
                "poly-scan binary %r not found on PATH; scraper calls will return empty",
                binary,
            )
            _warned_missing_binary = True
        return None

    cmd = [binary, *args]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_timeout_sec(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.warning("poly-scan timed out: %s", " ".join(cmd))
        return None
    except OSError as exc:
        log.warning("poly-scan failed to launch (%s): %s", exc, " ".join(cmd))
        return None

    if completed.returncode != 0:
        log.warning(
            "poly-scan exit %d for %s: %s",
            completed.returncode,
            " ".join(cmd),
            completed.stderr.strip(),
        )
        return None

    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        log.warning("poly-scan returned non-JSON for %s: %s", " ".join(cmd), exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def trends_limit_for_filters() -> int:
    """Choose ``N`` so ``poly-scan`` covers breakout + low-liquidity windows."""
    hrs = max(
        int(FILTERS["breakout_time_window_hrs"]),
        int(FILTERS["low_liquidity_dead_window_hrs"]),
    )
    # Lower bound so we always request a non-trivial series.
    return max(hrs * 4, 200)


def get_market_trends(market_id: str, limit: int) -> list[dict[str, Any]]:
    """Return oldest-first trend rows from polymarket-scraper for ``market_id``.

    The CLI returns newest-first; this function reverses to oldest-first to
    match the contract expected by ``evaluate_market_metrics``.
    """
    data = _run_poly_scan("get_market_trends", market_id, "--limit", str(limit))
    if not isinstance(data, list):
        return []
    return list(reversed(data))


def fetch_target_market_ids() -> list[str]:
    """Return market ids of open markets to feed the quantitative pipeline.

    The orchestrator pulls the freshest open markets (the CLI orders by
    most-recently-changed first) and lets phase 2 apply the deterministic
    quant filters. Limit is configurable via ``OPENCLAW_INGEST_LIMIT``.
    """
    limit = _ingest_limit()
    data = _run_poly_scan("get_open_markets", "--limit", str(limit))
    if not isinstance(data, list):
        return []

    ids: list[str] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        market_id = row.get("market_id")
        if isinstance(market_id, str) and market_id:
            ids.append(market_id)
    log.info("Ingested %d candidate market_ids", len(ids))
    return ids


def fetch_resolution(market_id: str) -> dict[str, Any] | None:
    """Return resolution metadata for ``market_id`` if the market has closed.

    Returns ``None`` for markets still trading (so the caller skips post-mortem).
    """
    market = _run_poly_scan("get_market", market_id)
    if not isinstance(market, dict):
        return None

    status = str(market.get("status") or "").strip().lower()
    if status not in _CLOSED_STATUSES:
        return None

    return {
        "market_id": market_id,
        "status": status,
        "outcome": market.get("outcome"),
        "resolution_source": market.get("resolution_source"),
        "latest_change": market.get("latest_change"),
        "raw": market,
    }


__all__ = [
    "trends_limit_for_filters",
    "get_market_trends",
    "fetch_target_market_ids",
    "fetch_resolution",
]
