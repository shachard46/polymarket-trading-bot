"""Merge Hub-side ``signal_bundle`` onto quantitative agent outputs.

Evaluator and Re-Evaluator LLMs return only decision fields; the orchestrator
re-invokes ``evaluate_market_metrics`` with the same inputs the agent's tool
call uses and attaches the bundle before vault persistence.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any, Callable

_QUANTITATIVE_ROLES = frozenset({"evaluator", "re_evaluator"})

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[1]
_SKILL_PATH = _ROOT / "skills/evaluate-market-metrics/evaluate_market_metrics.py"

_fetch_impl: Callable[[str, dict[str, Any] | None], dict[str, Any]] | None = None


def _load_evaluate_market_metrics():
    spec = importlib.util.spec_from_file_location(
        "evaluate_market_metrics",
        _SKILL_PATH,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load skill from {_SKILL_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.evaluate_market_metrics


def fetch_signal_bundle(
    market_id: str,
    filter_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call ``evaluate_market_metrics`` and return a plain dict."""
    global _fetch_impl
    if _fetch_impl is not None:
        return _fetch_impl(market_id, filter_overrides)

    evaluate = _load_evaluate_market_metrics()
    result = evaluate(market_id, filter_overrides)
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return dict(result)


def set_fetch_signal_bundle_impl(
    impl: Callable[[str, dict[str, Any] | None], dict[str, Any]] | None,
) -> None:
    """Override fetch for tests; pass ``None`` to restore the real skill."""
    global _fetch_impl
    _fetch_impl = impl


def attach_signal_bundle(
    role: str,
    payload: dict[str, Any],
    parsed: dict[str, Any],
) -> dict[str, Any]:
    """Attach ``signal_bundle`` for evaluator / re_evaluator outputs (no-op elsewhere)."""
    if role not in _QUANTITATIVE_ROLES:
        return parsed

    market_id = str(payload.get("market_id") or parsed.get("market_id") or "")
    if not market_id:
        log.warning("[%s] skipping signal_bundle merge: missing market_id", role)
        return parsed

    overrides = payload.get("filter_directives")
    if overrides is not None and not isinstance(overrides, dict):
        overrides = None

    try:
        bundle = fetch_signal_bundle(market_id, overrides)
    except Exception as exc:
        log.exception(
            "[%s] fetch_signal_bundle failed for %s: %s",
            role,
            market_id,
            exc,
        )
        bundle = {"market_id": market_id, "error": str(exc)}

    return {**parsed, "signal_bundle": bundle}


__all__ = [
    "attach_signal_bundle",
    "fetch_signal_bundle",
    "set_fetch_signal_bundle_impl",
]
