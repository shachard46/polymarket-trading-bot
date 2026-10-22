"""Fetch the ``evaluate_market_metrics`` signal bundle (a FEATURE source for the gate).

The skill computes volume_shock / breakout / info_drift / spread features from the
scraper DB. The deterministic gate (:mod:`orchestrator.gate`) uses them only as a
small ranking bonus; no agent consumes them any more.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parents[1]
_SKILL_PATH = _ROOT / "skills/evaluate-market-metrics/evaluate_market_metrics.py"

_fetch_impl: Callable[[str, dict[str, Any] | None], dict[str, Any]] | None = None
_evaluate_fn: Callable[..., Any] | None = None


def _load_evaluate_market_metrics():
    global _evaluate_fn
    if _evaluate_fn is not None:
        return _evaluate_fn
    spec = importlib.util.spec_from_file_location(
        "evaluate_market_metrics",
        _SKILL_PATH,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load skill from {_SKILL_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _evaluate_fn = mod.evaluate_market_metrics
    return _evaluate_fn


def fetch_signal_bundle(
    market_id: str,
    filter_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call ``evaluate_market_metrics`` and return a plain dict."""
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


__all__ = ["fetch_signal_bundle", "set_fetch_signal_bundle_impl"]
