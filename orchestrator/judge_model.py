"""Judge MODEL A/B (role ``deep_researcher``): which model judges a market, and what was actually used.

The arm comes from :func:`orchestrator.evidence.judge_model_arm`. The override itself is
:func:`orchestrator.llm.routing.model_override`; :func:`orchestrator.phases.agent_call.invoke_deep_researcher`
applies it per call and records the outcome here (including a fallback to the primary model), so ledger rows,
research frontmatter and logs show the provider/model that really answered.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from orchestrator import config, evidence

log = logging.getLogger(__name__)
ROLE = "deep_researcher"
FALLBACK_FLAG = "judge_model_fallback"

_lock = threading.Lock()
_used: dict[str, dict[str, Any]] = {}  # market_id -> {"arm", "fallback"} of the latest judge calls


def arm_for(market_id: str) -> str:
    return evidence.judge_model_arm(market_id)


def reset(market_id: str) -> None:
    with _lock:
        _used.pop(str(market_id), None)


def note_call(market_id: str, arm: str, fallback: bool) -> None:
    """Record one finished deep_researcher call; a fallback stays flagged until :func:`reset`."""
    with _lock:
        prev = _used.get(str(market_id)) or {}
        _used[str(market_id)] = {"arm": arm, "fallback": bool(fallback or prev.get("fallback"))}


def fell_back(market_id: str) -> bool:
    with _lock:
        return bool((_used.get(str(market_id)) or {}).get("fallback"))


def provider_model(market_id: str, fallback: bool | None = None) -> tuple[str | None, str | None]:
    """(provider, model) that judges ``market_id``: the arm's override unless that call fell back; None outside live mode."""
    from orchestrator.config import RUNNER_MODE_LIVE, runner_mode  # noqa: PLC0415
    from orchestrator.llm import routing  # noqa: PLC0415

    if runner_mode() != RUNNER_MODE_LIVE:
        return None, None
    if fallback is None:
        fallback = fell_back(market_id)
    try:
        if arm_for(market_id) == evidence.JUDGE_MODEL_DEEPSEEK and not fallback:
            spec = routing.parse_model_spec(config.judge_model_alt())
        else:
            spec = routing.spec_for(ROLE)
        return spec.provider, spec.model
    except Exception:  # noqa: BLE001 - never break a ledger write
        return None, None


def ledger_fields(market_id: str, fallback: bool | None = None) -> dict[str, Any]:
    """``judge_model_arm`` plus (live) the provider/model actually used, for ledger rows and the research frontmatter."""
    out: dict[str, Any] = {"judge_model_arm": arm_for(market_id)}
    out["provider"], out["model"] = provider_model(market_id, fallback)
    return out


def log_arm(market_id: str) -> None:
    provider, model = provider_model(market_id)
    label = f"{provider}:{model}" if provider else "stub"
    log.info("[PHASE 3] judge model arm for %s: %s (%s)", market_id, arm_for(market_id), label)
