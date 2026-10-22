"""Hub-side parallel A-IQ fetch for Phase 3 research bundles.

The orchestrator calls :func:`fetch_research_bundle`; agents never invoke
``execute_aiq_query`` directly. Worker threads load the skill module dynamically
and map all failures into per-query JSON payloads so the main thread never crashes.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from config.trading_constants import AIQ_CACHE_TTL_SEC
from orchestrator.config import RESEARCH_DATA_MAX_CHARS

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SKILL_PATH = _REPO_ROOT / "skills" / "execute-aiq-query" / "execute_aiq_query.py"
_MAX_WORKERS = 3
_TRUNCATION_SUFFIX = "\n...[truncated]"

_execute_aiq_query: Callable[[str], Any] | None = None


def reset_execute_aiq_query_cache() -> None:
    """Clear the cached skill callable (for tests)."""
    global _execute_aiq_query
    _execute_aiq_query = None


def _ensure_repo_on_path() -> None:
    root = str(_REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _load_execute_aiq_query() -> Callable[[str], Any]:
    """Load ``execute_aiq_query`` from the skill module (cached after first load)."""
    global _execute_aiq_query
    if _execute_aiq_query is not None:
        return _execute_aiq_query

    _ensure_repo_on_path()
    spec = importlib.util.spec_from_file_location(
        "execute_aiq_query_skill",
        _SKILL_PATH,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load skill module from {_SKILL_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn = getattr(module, "execute_aiq_query", None)
    if not callable(fn):
        raise ImportError("execute_aiq_query_skill missing execute_aiq_query")
    _execute_aiq_query = fn
    return fn


def _truncate_research_data(research_data: str) -> str:
    """Cap per-query payload size for multi-iteration Forensic Pipeline runs."""
    if len(research_data) <= RESEARCH_DATA_MAX_CHARS:
        return research_data
    keep = RESEARCH_DATA_MAX_CHARS - len(_TRUNCATION_SUFFIX)
    if keep < 0:
        return research_data[:RESEARCH_DATA_MAX_CHARS]
    return research_data[:keep] + _TRUNCATION_SUFFIX


def _run_single_query(query: str) -> dict[str, Any]:
    """Execute one A-IQ query; never raise — errors are embedded in the payload."""
    try:
        result = _load_execute_aiq_query()(query)
        research_data = getattr(result, "research_data", "") or ""
        err = getattr(result, "error", None)
        if not err and research_data:
            research_data = _truncate_research_data(research_data)
        return {
            "query": query,
            "research_data": research_data,
            "error": str(err) if err else None,
        }
    except Exception as exc:
        return {
            "query": query,
            "research_data": "",
            "error": str(exc),
        }


def fetch_research_bundle(queries: list[str]) -> list[dict[str, Any]]:
    """Fetch A-IQ results for ``queries`` in parallel, preserving input order.

    Each element is ``{"query", "research_data", "error"}``. Worker exceptions
    and HTTP/timeouts from the skill are caught per query and returned as
    ``error`` strings without aborting sibling fetches or the orchestrator thread.
    """
    if not queries:
        return []

    results: list[dict[str, Any] | None] = [None] * len(queries)

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        future_to_index = {
            pool.submit(_run_single_query, query): index
            for index, query in enumerate(queries)
        }
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                results[index] = {
                    "query": queries[index],
                    "research_data": "",
                    "error": str(exc),
                }

    return [entry for entry in results if entry is not None]


# ---------------------------------------------------------------------------
# Query cache (<vault>/02_Active_Research/aiq_cache.json)
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()


def normalize_query(query: str) -> str:
    """Cache key: lowercase with collapsed whitespace."""
    return " ".join(str(query).lower().split())


def _read_cache(path: Path) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_cache(path: Path, cache: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _fresh(entry: Any, now: float, ttl: float) -> bool:
    if not isinstance(entry, dict):
        return False
    try:
        age = now - float(entry["fetched_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return 0 <= age < ttl and bool(str(entry.get("research_data") or "").strip())


def fetch_research_bundle_cached(
    queries: list[str],
    cache_path: Path,
    *,
    fetch: Callable[[list[str]], list[dict[str, Any]]] = fetch_research_bundle,
    now: float | None = None,
    ttl_sec: float = AIQ_CACHE_TTL_SEC,
) -> list[dict[str, Any]]:
    """Like ``fetch`` but serves queries from a 24h on-disk cache; only misses hit A-IQ.

    Results keep input order and the caller's original query text. Rows with an error
    or empty data are never cached, so a failed fetch is retried next time.
    """
    if not queries:
        return []
    clock = time.time() if now is None else now
    with _cache_lock:
        cache = _read_cache(cache_path)
    out: list[dict[str, Any] | None] = [None] * len(queries)
    misses: dict[str, list[int]] = {}
    for i, query in enumerate(queries):
        key = normalize_query(query)
        entry = cache.get(key)
        if _fresh(entry, clock, ttl_sec):
            out[i] = {"query": query, "research_data": entry["research_data"], "error": None}
        else:
            misses.setdefault(key, []).append(i)

    if misses:
        first_text = [queries[idxs[0]] for idxs in misses.values()]
        fetched = fetch(first_text)
        for (key, idxs), row in zip(misses.items(), fetched):
            for i in idxs:
                out[i] = {**row, "query": queries[i]}
        with _cache_lock:
            cache = _read_cache(cache_path)
            cache = {k: v for k, v in cache.items() if _fresh(v, clock, ttl_sec)}
            for (key, idxs), row in zip(misses.items(), fetched):
                if not row.get("error") and str(row.get("research_data") or "").strip():
                    cache[key] = {"fetched_at": clock, "research_data": row["research_data"]}
            try:
                _write_cache(cache_path, cache)
            except OSError:
                pass  # the cache is an optimisation only
    return [row for row in out if row is not None]


__all__ = [
    "fetch_research_bundle",
    "fetch_research_bundle_cached",
    "normalize_query",
    "reset_execute_aiq_query_cache",
    "_MAX_WORKERS",
    "_truncate_research_data",
]
