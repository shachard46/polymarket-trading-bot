"""Phase 3 evidence sources: A-IQ vs the price-blind ``web_searcher`` agent (A/B capable).

Research must stay price-blind, so web results are sanitized: any line that mentions a
prediction market or betting odds is redacted before the bundle is written. Both arms write the
same ``vault.write_research_bundle`` format, so ``deep_researcher`` is unchanged.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Callable

from config.trading_constants import (
    BRAVE_QUERY_WORKERS,
    EVIDENCE_STALE_DAYS,
    GEMINI_QUERY_WORKERS,
    JUDGE_MODEL_ARM_SALT,
    SHADOW_RATE,
    SONNET_ARM_WEB_EFFORT,
    SONNET_ARM_WEB_MAX_USES,
)
from orchestrator import brave_search, config
from orchestrator.aiq_bundle import fetch_research_bundle_cached, normalize_query
from orchestrator.config import MAX_FORMAT_RETRIES, RESEARCH_DATA_MAX_CHARS
from orchestrator.util import truncate_lines
from orchestrator.web_filter import _URL_RE, NOT_FOUND, filter_rows, normalize_url
from orchestrator.parse import AgentOutputParseError, _role_keys, parse_agent_json_or_yaml
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


def _hash_parity(salt: str, market_id: str) -> int:
    return hashlib.sha256((salt + ":" + str(market_id)).encode("utf-8")).digest()[-1] % 2


def judge_arm(market_id: str) -> str:
    """Judge arm for a market: ``one`` | ``two`` (``JUDGE_ARM_MODE``; in ``ab`` the parity of a salted hash)."""
    mode = config.judge_arm_mode()
    if mode != config.JUDGE_ARM_AB:
        return mode
    return config.JUDGE_ARM_ONE if _hash_parity("judge_arm", market_id) == 0 else config.JUDGE_ARM_TWO


def judge_framing(market_id: str) -> str:
    """Framing (``yes`` | ``no``) of the single judge in the ``one`` arm (salted hash, independent of the arm)."""
    return "yes" if _hash_parity("judge_framing", market_id) == 0 else "no"


def is_shadow(market_id: str) -> bool:
    """Stable ~SHADOW_RATE selection of quick no_divergence skips for the shadow FULL run.

    Uses a salted hash prefix, so it is independent of the :func:`ab_arm` parity byte.
    """
    digest = hashlib.sha256(("shadow:" + str(market_id)).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 2**32 < SHADOW_RATE


JUDGE_MODEL_DEEPSEEK = "deepseek"
JUDGE_MODEL_SONNET = "sonnet"
JUDGE_MODEL_PRIMARY = "primary"


def judge_model_arm(market_id: str) -> str:
    """Judge MODEL arm: ``deepseek`` (PMBOT_JUDGE_MODEL_ALT), ``sonnet`` (PMBOT_JUDGE_MODEL_SONNET, judge + web), else ``primary``.

    Bands on one uniform value: [0, alt share) deepseek, [alt share, alt + sonnet share) sonnet, the rest primary.

    Uniform value from a salted sha256 prefix (own salt, independent of the other arms); ``PMBOT_JUDGE_MODEL_AB=off``
    makes every market ``primary``.
    """
    if not config.judge_model_ab_on():
        return JUDGE_MODEL_PRIMARY
    digest = hashlib.sha256((JUDGE_MODEL_ARM_SALT + ":" + str(market_id)).encode("utf-8")).hexdigest()
    u = int(digest[:8], 16) / 2**32
    alt = config.judge_model_alt_share()
    if u < alt:
        return JUDGE_MODEL_DEEPSEEK
    return JUDGE_MODEL_SONNET if u < alt + config.judge_model_sonnet_share() else JUDGE_MODEL_PRIMARY


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
    locale: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Web evidence rows for ``queries`` (aligned to the input, never raises except ``InfraUnavailable``).

    Backend (``config.web_backend()``): Brave search + page fetch + ``web_extractor``, or the gemini
    ``web_searcher`` (one query per call, in parallel). A query whose Brave path errors falls back to gemini.
    ``locale`` (``{"country", "language"}``, local-language queries) is passed to Brave only.
    Sonnet-arm markets (live) first try the web_searcher under the sonnet model (:func:`_sonnet_rows`), instead of Brave.
    """
    sonnet = _sonnet_rows(runner, market_id, queries, today)
    if sonnet is not None:
        _log_pages(market_id, "sonnet", sonnet)
        return sonnet
    if config.web_backend() == config.WEB_BACKEND_BRAVE:
        rows = (
            _brave_rows(runner, market_id, queries, today, locale) if locale
            else _brave_rows(runner, market_id, queries, today)
        )
        backends = [config.WEB_BACKEND_BRAVE] * len(rows)
        failed = [i for i, r in enumerate(rows) if r.get("error")]
        if failed:
            log.warning(
                "[WEB-FALLBACK] %s: brave path failed for %d/%d queries (%s); using gemini web_searcher",
                market_id, len(failed), len(queries), _failure_reasons([rows[i] for i in failed]),
            )
            fallback = _gemini_rows(runner, market_id, [queries[i] for i in failed], today)
            for i, r in zip(failed, fallback):
                rows[i] = r
                backends[i] = config.WEB_BACKEND_GEMINI
        backend = _backend_label(backends)
    else:
        rows, backend = _gemini_rows(runner, market_id, queries, today), config.WEB_BACKEND_GEMINI
    _log_pages(market_id, backend, rows)
    return rows


def _sonnet_rows(
    runner: Callable[[str, dict[str, Any]], Any],
    market_id: str,
    queries: list[str],
    today: str,
) -> list[dict[str, Any]] | None:
    """Sonnet arm (live only): per-query ``web_searcher`` calls under the sonnet override, same filter/sanitize as gemini.

    None = not a sonnet-arm market, or the sonnet path errored / gave no usable rows (flagged ``web_model_fallback``):
    the caller runs the normal backend.
    """
    if judge_model_arm(market_id) != JUDGE_MODEL_SONNET or config.runner_mode() != config.RUNNER_MODE_LIVE:
        return None
    from orchestrator import judge_model  # noqa: PLC0415 - judge_model imports this module
    from orchestrator.llm import routing  # noqa: PLC0415

    model = config.judge_model_sonnet()
    rows: list[dict[str, Any]] = []
    try:
        with routing.model_override(
            judge_model.WEB_ROLE, model, effort=SONNET_ARM_WEB_EFFORT, web_max_uses=SONNET_ARM_WEB_MAX_USES
        ):
            rows = _gemini_rows(runner, market_id, queries, today)
    except InfraUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - fall back to the normal path
        log.warning("[WEB-FALLBACK] %s: sonnet web search failed (%s)", market_id, exc)
    if rows_usable(rows):
        judge_model.note_web(market_id, model, False)
        return rows
    log.warning("[WEB-FALLBACK] %s: sonnet web search gave no usable rows; using the normal web path", market_id)
    judge_model.note_web(market_id, None, True)
    return None


def _failure_reasons(rows: list[dict[str, Any]]) -> str:
    """The distinct real errors behind failed Brave-path rows, tagged by stage (search = Brave itself, else the extractor)."""
    seen: dict[str, int] = {}
    for r in rows:
        err = str(r.get("error") or "unknown").strip()
        stage = "search" if err.startswith("brave") else "extractor"
        key = f"{stage}: {err[:160]}"
        seen[key] = seen.get(key, 0) + 1
    return "; ".join(f"{k} x{n}" if n > 1 else k for k, n in seen.items())


def _backend_label(backends: list[str]) -> str:
    """``brave``, ``gemini``, or ``brave+gemini(fallback=N)`` when some rows were replaced by the gemini fallback."""
    n_gemini = sum(1 for b in backends if b == config.WEB_BACKEND_GEMINI)
    if n_gemini == 0:
        return config.WEB_BACKEND_BRAVE
    if n_gemini == len(backends):
        return config.WEB_BACKEND_GEMINI
    return f"{config.WEB_BACKEND_BRAVE}+{config.WEB_BACKEND_GEMINI}(fallback={n_gemini})"


def _count_urls(text: str) -> int:
    """Distinct URLs in a row's fact lines (the pages that actually back the kept facts)."""
    return len({normalize_url(m.group(0)) for m in _URL_RE.finditer(text or "")})


def _log_pages(market_id: str, backend: str, rows: list[dict[str, Any]]) -> None:
    counts = [_count_urls(r.get("research_data") or "") if row_usable(r) else 0 for r in rows]
    avg = sum(counts) / len(counts) if counts else 0.0
    log.info(
        "[WEB-PAGES] %s backend=%s queries=%d pages_per_query=%s avg=%.2f empty=%d",
        market_id, backend, len(counts), counts, avg, sum(1 for c in counts if c == 0),
    )


def _run_parallel(fn: Callable[[Any], Any], items: list[Any], workers: int) -> list[Any]:
    """``fn`` over ``items`` in a thread pool, order kept; each call gets a copy of the caller's context."""
    if len(items) <= 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as pool:
        futures = [pool.submit(contextvars.copy_context().run, fn, i) for i in items]
        return [f.result() for f in futures]


def _gemini_rows(
    runner: Callable[[str, dict[str, Any]], Any],
    market_id: str,
    queries: list[str],
    today: str,
) -> list[dict[str, Any]]:
    """Gemini backend: ONE query per ``web_searcher`` call (so each gets the full search budget), in parallel."""
    if len(queries) <= 1:
        return _gemini_one(runner, market_id, list(queries), today)
    per_query = _run_parallel(
        lambda q: _gemini_one(runner, market_id, [q], today), list(queries), GEMINI_QUERY_WORKERS
    )
    return [rows[0] for rows in per_query]


def _gemini_one(
    runner: Callable[[str, dict[str, Any]], Any],
    market_id: str,
    queries: list[str],
    today: str,
) -> list[dict[str, Any]]:
    """One ``web_searcher`` call; if no row is usable (errors, wrong shape, only ``NOT_FOUND``) retried once."""
    rows = _fetch_web_rows_once(runner, market_id, queries, today)
    if queries and not rows_usable(rows):
        log.warning("[WEB-RETRY] %s: no usable web evidence; retrying web_searcher once", market_id)
        rows = _fetch_web_rows_once(runner, market_id, queries, today)
    return rows


def _agent_rows(
    runner: Callable[[str, dict[str, Any]], Any],
    role: str,
    payload: dict[str, Any],
    market_id: str,
    queries: list[str],
) -> list[dict[str, Any]]:
    """One ``web_searcher``-schema agent call; unfiltered rows aligned to ``queries``, never raises."""

    def fail(msg: str) -> list[dict[str, Any]]:
        return [{"query": q, "research_data": "", "error": msg} for q in queries]

    parsed: dict[str, Any] | None = None
    attempt_payload = payload
    for attempt in range(MAX_FORMAT_RETRIES + 1):
        last = attempt >= MAX_FORMAT_RETRIES
        try:
            raw = runner(role, attempt_payload)
        except AgentSchemaError as exc:
            if exc.direction != "output" or last:
                return fail(f"{role} schema mismatch: {exc.cause}")
            attempt_payload = {**payload, "format_validation_error": str(exc)}
            continue
        except InfraUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            return fail(f"{role} failed: {exc}")
        try:
            parsed = raw if isinstance(raw, dict) else parse_agent_json_or_yaml(raw, expected_keys=_role_keys(role))
            break
        except AgentOutputParseError as exc:
            if last:
                return fail(f"{role} parse error: {exc}")
            attempt_payload = {**payload, "format_validation_error": str(exc)}
    assert parsed is not None
    results = _salvage_results(parsed, queries)
    if results is None:
        if parsed.get("error"):
            return fail(f"{role} error: {parsed['error']}")
        results = parsed.get("results") or []
    by_query = {normalize_query(r.get("query", "")): r for r in results if isinstance(r, dict)}
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
    return rows


def _record_filter(market_id: str, model: str, counts: Any) -> None:
    log.info(
        "[WEB-FILTER] %s model=%s kept=%d %s", market_id, model, counts.kept, counts.reason(),
    )
    from orchestrator.llm import usage  # noqa: PLC0415

    usage.record_web_filter(
        model, counts.kept, counts.dropped_no_url, counts.dropped_unretrieved, market_id=market_id,
        dropped_unverifiable=counts.dropped_unverifiable, not_found=counts.not_found,
        dropped_blank=counts.dropped_blank,
    )


def _fetch_web_rows_once(
    runner: Callable[[str, dict[str, Any]], Any],
    market_id: str,
    queries: list[str],
    today: str,
) -> list[dict[str, Any]]:
    """One gemini ``web_searcher`` call and its filtered, sanitized rows."""
    from orchestrator import direct_runner  # noqa: PLC0415 - lazy: keeps provider SDKs out of stub mode

    direct_runner.reset_last_call()  # a stale value from an earlier call must never be read
    rows = _agent_rows(
        runner, "web_searcher", {"market_id": market_id, "queries": list(queries), "today": today},
        market_id, queries,
    )
    # last_model() is None when no real provider call happened (stub / injected runner): nothing to verify.
    web_call = direct_runner.last_model() is not None
    rows, counts = filter_rows(rows, direct_runner.last_citations(), strict=web_call)
    _record_filter(market_id, direct_runner.last_model() or "stub", counts)
    return sanitize_rows(rows)


# ---- Brave backend --------------------------------------------------------------------------------

BRAVE_MODEL = "brave+web_extractor"
PAGES_KEY = "pages"  # in-memory only: [{url, text}] the extractor read for this row
_UNKNOWN_DATE_TAIL_RE = re.compile(r"\|\s*unknown\s*$", re.IGNORECASE)


def _fill_dates(text: str, ages: dict[str, str]) -> str:
    """Replace a trailing ``| unknown`` date with Brave's page_age for that line's URL, when it has one."""
    out = []
    for line in text.splitlines():
        m = _URL_RE.search(line)
        age = ages.get(normalize_url(m.group(0))) if m else None
        out.append(_UNKNOWN_DATE_TAIL_RE.sub(f"| {age}", line) if age and _UNKNOWN_DATE_TAIL_RE.search(line) else line)
    return "\n".join(out)


def _log_page_diagnostics(market_id: str, query: str, pages: list[Any]) -> None:
    """Why the extractor may have found nothing: per page host, fetched-or-snippet-only, text size, text head."""
    log.warning(
        "[WEB-DIAG] %s query_sent=%r pages=%s", market_id, brave_search.keyword_query(query),
        [
            {
                "host": (urlsplit(p.url).hostname or "")[:60], "fetched": bool(getattr(p, "fetched", None)),
                "chars": len(p.text), "head": " ".join(p.text.split())[:80],
            }
            for p in pages
        ],
    )


def _brave_one(
    runner: Callable[[str, dict[str, Any]], Any], market_id: str, query: str, today: str,
    locale: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        if locale:
            pages = brave_search.search_pages(
                query, country=locale.get("country"), search_lang=locale.get("language")
            )
        else:
            pages = brave_search.search_pages(query)
    except brave_search.BraveError as exc:
        return {"query": query, "research_data": "", "error": f"brave: {exc}"}
    except Exception as exc:  # noqa: BLE001 - any search/fetch bug must fall back, not abort the tick
        return {"query": query, "research_data": "", "error": f"brave failed: {exc}"}
    if not pages:
        return {"query": query, "research_data": NOT_FOUND, "error": None}
    payload = {
        "market_id": market_id,
        "query": query,
        "today": today,
        "pages": [{"url": p.url, "title": p.title, "page_age": p.page_age, "text": p.text} for p in pages],
    }
    (row,) = _agent_rows(runner, "web_extractor", payload, market_id, [query])
    row = {**row, "pages_fetched": len(pages)}
    if row.get("error") or not str(row.get("research_data") or "").strip():
        return row
    # page text rides along in memory only (the auditor reads it); callers strip it before persisting
    page_texts = [{"url": p.url, "text": p.text} for p in pages]
    ages = {normalize_url(p.url): p.page_age for p in pages if p.page_age}
    row["research_data"] = _fill_dates(row["research_data"], ages)
    # the pages handed to the extractor are exactly the retrieved set: airtight citation guard
    (row,), counts = filter_rows([row], [p.url for p in pages], strict=True)
    _record_filter(market_id, BRAVE_MODEL, counts)
    if counts.kept == 0:
        _log_page_diagnostics(market_id, query, pages)
        # NOT_FOUND is the extractor finding nothing relevant on the pages (not a guard drop): say so.
        log.warning(
            "[WEB-GUARD] %s: no extractor line kept for %r (%d pages): %s",
            market_id, query, len(pages), counts.reason(),
        )
    return {**row, PAGES_KEY: page_texts}


def _brave_rows(
    runner: Callable[[str, dict[str, Any]], Any],
    market_id: str,
    queries: list[str],
    today: str,
    locale: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rows = _run_parallel(
        lambda q: _brave_one(runner, market_id, q, today, locale), list(queries), BRAVE_QUERY_WORKERS
    )
    log.info(
        "[BRAVE-PAGES] %s pages_fetched_per_query=%s", market_id, [r.get("pages_fetched", 0) for r in rows]
    )
    return sanitize_rows([{k: v for k, v in r.items() if k != "pages_fetched"} for r in rows])


# ---- Labelled evidence (A1) -----------------------------------------------------------------------

_DATE_TAIL_RE = re.compile(r"\|\s*(\d{4}-\d{2}-\d{2})\s*(?:\(stale\))?\s*$")
_NO_LABEL_PREFIXES = (REDACTED, NOT_FOUND, "[truncated")


def _line_age_days(line: str, today: date | None) -> int | None:
    m = _DATE_TAIL_RE.search(line)
    if not m or today is None:
        return None
    try:
        return max(0, (today - date.fromisoformat(m.group(1))).days)
    except ValueError:
        return None


def _today_date(today: str) -> date | None:
    try:
        return date.fromisoformat(str(today)[:10])
    except ValueError:
        return None


def _row_lines(row: dict[str, Any]) -> list[str] | None:
    """The row's text lines, or ``None`` for an error/empty row (never labelled)."""
    data = str(row.get("research_data") or "")
    if row.get("error") or not data.strip():
        return None
    return data.splitlines()


def _is_evidence_line(line: str) -> bool:
    return bool(line.strip()) and not line.strip().startswith(_NO_LABEL_PREFIXES)


def line_url(line: str) -> str | None:
    m = _URL_RE.search(line)
    return normalize_url(m.group(0)) if m else None


def _label(n: int, age: int | None, quality: str | None, unverified: bool) -> str:
    """``[E<n> | <age>d | STALE | <quality> | unverified]``; the tags after the age appear only with a quality tag."""
    parts = [f"E{n}", "undated" if age is None else f"{age}d"]
    if age is not None and age > EVIDENCE_STALE_DAYS:
        parts.append("STALE")
    if quality is not None:
        parts.append(quality)
    if quality is not None and unverified:
        parts.append("unverified")
    return "[" + " | ".join(parts) + "]"


def label_bundle(
    bundle: list[dict[str, Any]], today: str, *, quality: bool = False, only_ids: set[str] | None = None
) -> list[dict[str, Any]]:
    """Copy of ``bundle`` whose evidence lines carry ``[E<n> | <age>d]`` / ``[E<n> | undated]`` labels.

    Rendering only: stored bundles are never mutated. Ids run across the whole bundle. The age is days between
    ``today`` and the line's trailing ``| YYYY-MM-DD`` field; lines with no such date (all A-IQ prose) are
    ``undated``. Age above ``EVIDENCE_STALE_DAYS`` adds ``STALE``. Error rows and empty/NOT_FOUND/marker lines
    are left unlabelled. With ``quality=True`` each label also carries the source-quality tag of the line's URL
    (``unknown`` with no URL) and ``unverified`` unless the audit verified that line (row key ``verified``: line indexes).
    With ``only_ids`` (E-ids such as ``"E7"``) the ids are still assigned over the whole bundle, but evidence lines
    outside the set are omitted (a row left with no evidence line is omitted): the judge's trimmed input.
    """
    from orchestrator.source_quality import quality_for  # noqa: PLC0415 - keeps yaml load off the import path

    today_d = _today_date(today)
    n = 0
    out = []
    for row in bundle or []:
        src = _row_lines(row)
        if src is None:
            out.append(row)
            continue
        lines = []
        kept_any = False
        had_any = False
        verified = set(row.get("verified") or [])
        for li, line in enumerate(src):
            if not _is_evidence_line(line):
                lines.append(line)
                continue
            n += 1
            had_any = True
            if only_ids is not None and f"E{n}" not in only_ids:
                continue
            kept_any = True
            m = _URL_RE.search(line)
            tier = (quality_for(m.group(0)) if m else "unknown") if quality else None
            lines.append(f"{_label(n, _line_age_days(line, today_d), tier, li not in verified)} {line}")
        if only_ids is not None and had_any and not kept_any:
            continue
        out.append({**row, "research_data": "\n".join(lines)})
    return out


def label_lines(bundle: list[dict[str, Any]], today: str) -> list[dict[str, Any]]:
    """One record per labelled evidence line, with the same ``E<n>`` ids :func:`label_bundle`` assigns.

    Keys: ``id`` (``"E7"``), ``row`` (index in ``bundle``), ``line`` (index within the row's lines),
    ``crux_id``, ``role``, ``text``, ``url`` (normalized or None), ``age_days`` (None = undated), ``stale``, ``quality``.
    """
    from orchestrator.source_quality import quality_for  # noqa: PLC0415

    today_d = _today_date(today)
    n = 0
    out: list[dict[str, Any]] = []
    for ri, row in enumerate(bundle or []):
        src = _row_lines(row)
        if src is None:
            continue
        for li, line in enumerate(src):
            if not _is_evidence_line(line):
                continue
            n += 1
            m = _URL_RE.search(line)
            age = _line_age_days(line, today_d)
            out.append({
                "id": f"E{n}", "row": ri, "line": li, "crux_id": row.get("crux_id"), "role": row.get("role"),
                "text": line, "url": normalize_url(m.group(0)) if m else None, "age_days": age,
                "stale": age is not None and age > EVIDENCE_STALE_DAYS,
                "quality": quality_for(m.group(0)) if m else "unknown",
            })
    return out


def fetch_web_bundle(
    runner: Callable[[str, dict[str, Any]], Any],
    vault: Any,
    market_id: str,
    queries: list[str],
    today: str,
    locale: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return fetch_research_bundle_cached(
        queries,
        web_cache_path(vault),
        fetch=lambda qs: (
            fetch_web_rows(runner, market_id, qs, today, locale) if locale
            else fetch_web_rows(runner, market_id, qs, today)
        ),
    )


__all__ = [
    "AIQ",
    "WEB",
    "ab_arm",
    "judge_arm",
    "judge_framing",
    "judge_model_arm",
    "is_shadow",
    "label_lines",
    "fetch_web_bundle",
    "fetch_web_rows",
    "intended_source",
    "label_bundle",
    "rows_usable",
    "sanitize_rows",
    "sanitize_text",
    "web_cache_path",
]
