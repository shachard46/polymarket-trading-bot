"""Hub-side parallel A-IQ fetch for Phase 3 research bundles.

The orchestrator calls :func:`fetch_research_bundle`; agents never invoke
``execute_aiq_query`` directly. Worker threads load the skill module dynamically
and map all failures into per-query JSON payloads so the main thread never crashes.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from config.trading_constants import AIQ_CACHE_TTL_SEC
from obsidian_utils import read_json, write_json_atomic
from orchestrator.config import RESEARCH_DATA_MAX_CHARS
from orchestrator.util import truncate_lines

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SKILL_PATH = _REPO_ROOT / "skills" / "execute-aiq-query" / "execute_aiq_query.py"
_MAX_WORKERS = 3

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
    return truncate_lines(research_data, RESEARCH_DATA_MAX_CHARS)


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


class _Flight:
    """An in-flight fetch of one query; waiters block on ``event`` and read ``row``."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.row: dict[str, Any] | None = None


_inflight: dict[tuple[str, str], _Flight] = {}


def normalize_query(query: str) -> str:
    """Cache key: lowercase with collapsed whitespace."""
    return " ".join(str(query).lower().split())


def _read_cache(path: Path) -> dict[str, dict[str, Any]]:
    raw = read_json(path)
    return raw if isinstance(raw, dict) else {}


def _write_cache(path: Path, cache: dict[str, dict[str, Any]]) -> None:
    write_json_atomic(path, cache, indent=1, sort_keys=True)


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
    out: list[dict[str, Any] | None] = [None] * len(queries)
    misses: dict[str, list[int]] = {}
    owned: dict[str, _Flight] = {}  # keys this call fetches itself
    waiting: dict[str, _Flight] = {}  # keys another thread is already fetching
    with _cache_lock:
        cache = _read_cache(cache_path)
        for i, query in enumerate(queries):
            key = normalize_query(query)
            entry = cache.get(key)
            if _fresh(entry, clock, ttl_sec):
                out[i] = {"query": query, "research_data": entry["research_data"], "error": None}
                continue
            misses.setdefault(key, []).append(i)
            if key in owned or key in waiting:
                continue
            flight = _inflight.get((str(cache_path), key))
            if flight is None:
                flight = _inflight[(str(cache_path), key)] = _Flight()
                owned[key] = flight
            else:
                waiting[key] = flight

    if owned:
        keys = list(owned)
        rows: dict[str, dict[str, Any]] = {}
        try:
            fetched = fetch([queries[misses[k][0]] for k in keys])
            rows = dict(zip(keys, fetched))
        finally:
            with _cache_lock:
                try:
                    cache = _read_cache(cache_path)
                    cache = {k: v for k, v in cache.items() if _fresh(v, clock, ttl_sec)}
                    for key, row in rows.items():
                        if not row.get("error") and str(row.get("research_data") or "").strip():
                            cache[key] = {"fetched_at": clock, "research_data": row["research_data"]}
                    try:
                        _write_cache(cache_path, cache)
                    except OSError:
                        pass  # the cache is an optimisation only
                finally:
                    for key, flight in owned.items():
                        flight.row = rows.get(key)
                        flight.event.set()
                        _inflight.pop((str(cache_path), key), None)
        for key in keys:
            for i in misses[key]:
                out[i] = {**rows[key], "query": queries[i]}

    for key, flight in waiting.items():
        flight.event.wait()
        row = flight.row or {"research_data": "", "error": "in-flight fetch failed"}
        for i in misses[key]:
            out[i] = {**row, "query": queries[i]}
    return [row for row in out if row is not None]


__all__ = [
    "fetch_research_bundle",
    "fetch_research_bundle_cached",
    "normalize_query",
    "reset_execute_aiq_query_cache",
    "_MAX_WORKERS",
    "_truncate_research_data",
]
