"""Phase 3 evidence sources: A-IQ vs the price-blind ``web_searcher`` agent (A/B capable).

Research must stay price-blind, so web results are sanitized: any line that mentions a
prediction market or betting odds is redacted before the bundle is written. Both arms write the
same ``vault.write_research_bundle`` format, so ``deep_researcher`` is unchanged.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Callable

from orchestrator import config
from orchestrator.aiq_bundle import fetch_research_bundle_cached, normalize_query
from orchestrator.config import RESEARCH_DATA_MAX_CHARS
from orchestrator.parse import AgentOutputParseError, parse_agent_json_or_yaml
from orchestrator.schema_validation import AgentSchemaError
from orchestrator.state import InfraUnavailable

log = logging.getLogger(__name__)

AIQ = config.EVIDENCE_AIQ
WEB = config.EVIDENCE_WEB
REDACTED = "[redacted: market/odds mention]"

# Prediction-market venues / bookmakers and betting-odds phrasing.
_MARKET_RE = re.compile(
    r"polymarket|kalshi|predictit|manifold|metaculus|augur|betfair|bet365|draftkings|fanduel|"
    r"pinnacle|william\s*hill|paddy\s*power|bookmaker|sportsbook|oddsmaker|"
    r"prediction\s+markets?|betting\s+(?:odds|markets?|lines?)|"
    r"\bodds\b|implied\s+probabilit|moneyline|market[- ]implied|"
    r"\b(?:yes|no)\s+shares?\b|traders?\s+(?:give|price|put|see)",
    re.IGNORECASE,
)


def ab_arm(market_id: str) -> str:
    """Stable A/B arm for a market: parity of sha256(market_id) (even -> aiq, odd -> web)."""
    digest = hashlib.sha256(str(market_id).encode("utf-8")).digest()
    return AIQ if digest[-1] % 2 == 0 else WEB


def intended_source(market_id: str) -> str:
    mode = config.evidence_source()
    return ab_arm(market_id) if mode == config.EVIDENCE_AB else mode


def sanitize_text(text: str) -> str:
    """Redact every line that mentions prediction markets or betting odds."""
    return "\n".join(
        REDACTED if _MARKET_RE.search(line) else line for line in str(text or "").splitlines()
    )


def sanitize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        data = sanitize_text(r.get("research_data") or "")[:RESEARCH_DATA_MAX_CHARS]
        out.append({**r, "research_data": data})
    return out


def rows_usable(rows: list[dict[str, Any]]) -> bool:
    return any(str(r.get("research_data") or "").strip() and not r.get("error") for r in rows)


def web_cache_path(vault: Any) -> Path:
    return vault._dirs["active"] / "web_cache.json"


def fetch_web_rows(
    runner: Callable[[str, dict[str, Any]], Any],
    market_id: str,
    queries: list[str],
    today: str,
) -> list[dict[str, Any]]:
    """One ``web_searcher`` call for ``queries``; rows aligned to the input, never raises."""

    def fail(msg: str) -> list[dict[str, Any]]:
        return [{"query": q, "research_data": "", "error": msg} for q in queries]

    try:
        raw = runner("web_searcher", {"market_id": market_id, "queries": list(queries), "today": today})
    except AgentSchemaError as exc:
        return fail(f"web_searcher schema mismatch: {exc.cause}")
    except InfraUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        return fail(f"web_searcher failed: {exc}")
    try:
        parsed = raw if isinstance(raw, dict) else parse_agent_json_or_yaml(raw)
    except AgentOutputParseError as exc:
        return fail(f"web_searcher parse error: {exc}")
    if parsed.get("error"):
        return fail(f"web_searcher error: {parsed['error']}")
    by_query = {
        normalize_query(r.get("query", "")): r
        for r in parsed.get("results") or []
        if isinstance(r, dict)
    }
    rows = []
    for q in queries:
        r = by_query.get(normalize_query(q))
        if r is None:
            rows.append({"query": q, "research_data": "", "error": "no result for query"})
        else:
            err = r.get("error")
            rows.append(
                {
                    "query": q,
                    "research_data": str(r.get("research_data") or ""),
                    "error": str(err) if err else None,
                }
            )
    return sanitize_rows(rows)


def fetch_web_bundle(
    runner: Callable[[str, dict[str, Any]], Any],
    vault: Any,
    market_id: str,
    queries: list[str],
    today: str,
) -> list[dict[str, Any]]:
    return fetch_research_bundle_cached(
        queries,
        web_cache_path(vault),
        fetch=lambda qs: fetch_web_rows(runner, market_id, qs, today),
    )


__all__ = [
    "AIQ",
    "WEB",
    "ab_arm",
    "fetch_web_bundle",
    "fetch_web_rows",
    "intended_source",
    "rows_usable",
    "sanitize_rows",
    "sanitize_text",
    "web_cache_path",
]
