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

    out = dict(parsed)
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
        out["passed"] = False
        out["error"] = f"signal_bundle fetch failed: {exc}"
        out["details"] = f"{out.get('details') or ''} [signal_bundle fetch failed: {exc}]".strip()
        out["signal_bundle"] = bundle
        return out

    veto = bundle.get("hard_veto") if isinstance(bundle, dict) else None
    if isinstance(veto, dict) and veto.get("passed") is not None:
        v_passed = bool(veto["passed"])
        v_trigger = veto.get("trigger")
        if out.get("passed") != v_passed or out.get("trigger") != v_trigger:
            log.warning(
                "[%s] overriding agent decision for %s to match hard_veto=%r "
                "(agent passed=%r trigger=%r)",
                role,
                market_id,
                veto,
                out.get("passed"),
                out.get("trigger"),
            )
        out["passed"] = v_passed
        out["trigger"] = v_trigger

    out["signal_bundle"] = bundle
    return out


__all__ = [
    "attach_signal_bundle",
    "fetch_signal_bundle",
    "set_fetch_signal_bundle_impl",
]
