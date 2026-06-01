"""Hub-side parallel A-IQ fetch for Phase 3 research bundles.

The orchestrator calls :func:`fetch_research_bundle`; agents never invoke
``execute_aiq_query`` directly. Worker threads load the skill module dynamically
and map all failures into per-query JSON payloads so the main thread never crashes.
"""

from __future__ import annotations

import importlib.util
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

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


def _run_single_query(query: str) -> dict[str, Any]:
    """Execute one A-IQ query; never raise — errors are embedded in the payload."""
    try:
        result = _load_execute_aiq_query()(query)
        research_data = getattr(result, "research_data", "") or ""
        err = getattr(result, "error", None)
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


__all__ = [
    "fetch_research_bundle",
    "reset_execute_aiq_query_cache",
    "_MAX_WORKERS",
]
