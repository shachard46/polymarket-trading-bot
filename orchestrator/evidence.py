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

from config.trading_constants import INDEP_SAMPLE_RATE, SHADOW_RATE
from orchestrator import config
from orchestrator.aiq_bundle import fetch_research_bundle_cached, normalize_query
from orchestrator.config import RESEARCH_DATA_MAX_CHARS
from orchestrator.util import truncate_lines
from orchestrator.web_filter import NOT_FOUND, filter_rows
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


def is_shadow(market_id: str) -> bool:
    """Stable ~SHADOW_RATE selection of quick no_divergence skips for the shadow FULL run.

    Uses a salted hash prefix, so it is independent of the :func:`ab_arm` parity byte.
    """
    digest = hashlib.sha256(("shadow:" + str(market_id)).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 2**32 < SHADOW_RATE


def is_indep_sample(market_id: str) -> bool:
    """Stable ~INDEP_SAMPLE_RATE selection for the measure-only independent-evidence sample.

    Salted hash prefix: independent of :func:`ab_arm` and :func:`is_shadow`.
    """
    digest = hashlib.sha256(("indep:" + str(market_id)).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 2**32 < INDEP_SAMPLE_RATE


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
        data = truncate_lines(sanitize_text(r.get("research_data") or ""), RESEARCH_DATA_MAX_CHARS)
        out.append({**r, "research_data": data})
    return out


def row_usable(row: dict[str, Any]) -> bool:
    """A row is evidence only with data, no error, and not a bare ``NOT_FOUND``."""
    data = str(row.get("research_data") or "").strip()
    return bool(data) and data != NOT_FOUND and not row.get("error")


def rows_usable(rows: list[dict[str, Any]]) -> bool:
    return any(row_usable(r) for r in rows)


def web_cache_path(vault: Any) -> Path:
    return vault._dirs["active"] / "web_cache.json"


def _salvage_results(parsed: dict[str, Any], queries: list[str]) -> list[dict[str, Any]] | None:
    """Wrap a wrong-shaped answer (top-level ``research_data``, no ``results`` list) as one result.

    The query is the answer's own ``query``, else the only query asked, else the asked query whose
    text occurs in the answer; with none of those the result cannot be placed and is not salvaged.
    """
    if isinstance(parsed.get("results"), list) or parsed.get("research_data") is None:
        return None
    data = str(parsed.get("research_data") or "")
    query = str(parsed.get("query") or "").strip()
    asked = {normalize_query(q): q for q in queries}
    if normalize_query(query) in asked:
        query = asked[normalize_query(query)]
    elif len(queries) == 1:
        query = queries[0]
    else:
        hits = [q for q in queries if normalize_query(q) and normalize_query(q) in normalize_query(data)]
        if not hits:
            return None
        query = hits[0]
    return [{"query": query, "research_data": data, "error": parsed.get("error") or None}]


def fetch_web_rows(
    runner: Callable[[str, dict[str, Any]], Any],
    market_id: str,
    queries: list[str],
    today: str,
) -> list[dict[str, Any]]:
    """``web_searcher`` call for ``queries``; rows aligned to the input, never raises.

    If no row is usable (errors, wrong shape, only ``NOT_FOUND``) the call is retried once.
    """
    rows = _fetch_web_rows_once(runner, market_id, queries, today)
    if queries and not rows_usable(rows):
        log.warning("[WEB-RETRY] %s: no usable web evidence; retrying web_searcher once", market_id)
        rows = _fetch_web_rows_once(runner, market_id, queries, today)
    return rows


def _fetch_web_rows_once(
    runner: Callable[[str, dict[str, Any]], Any],
    market_id: str,
    queries: list[str],
    today: str,
) -> list[dict[str, Any]]:
    """One ``web_searcher`` call and its filtered, sanitized rows."""

    def fail(msg: str) -> list[dict[str, Any]]:
        return [{"query": q, "research_data": "", "error": msg} for q in queries]

    from orchestrator import direct_runner  # noqa: PLC0415 - lazy: keeps provider SDKs out of stub mode

    direct_runner.reset_last_call()  # a stale value from an earlier call must never be read
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
    results = _salvage_results(parsed, queries)
    if results is None:
        if parsed.get("error"):
            return fail(f"web_searcher error: {parsed['error']}")
        results = parsed.get("results") or []
    by_query = {
        normalize_query(r.get("query", "")): r
        for r in results
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
    # last_model() is None when no real provider call happened (stub / injected runner): nothing to verify.
    web_call = direct_runner.last_model() is not None
    rows, counts = filter_rows(rows, direct_runner.last_citations(), strict=web_call)
    model = direct_runner.last_model() or "stub"
    log.info(
        "[WEB-FILTER] %s model=%s kept=%d dropped_no_url=%d dropped_unretrieved=%d dropped_unverifiable=%d",
        market_id, model, counts.kept, counts.dropped_no_url, counts.dropped_unretrieved,
        counts.dropped_unverifiable,
    )
    from orchestrator.llm import usage  # noqa: PLC0415

    usage.record_web_filter(
        model, counts.kept, counts.dropped_no_url, counts.dropped_unretrieved, market_id=market_id,
        dropped_unverifiable=counts.dropped_unverifiable,
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
    "is_shadow",
    "is_indep_sample",
    "fetch_web_bundle",
    "fetch_web_rows",
    "intended_source",
    "rows_usable",
    "sanitize_rows",
    "sanitize_text",
    "web_cache_path",
]
