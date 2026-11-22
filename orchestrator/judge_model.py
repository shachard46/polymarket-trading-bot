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
WEB_ROLE = "web_searcher"
WEB_FALLBACK_FLAG = "web_model_fallback"

_lock = threading.Lock()
_used: dict[str, dict[str, Any]] = {}  # market_id -> {"arm", "fallback"} of the latest judge calls
# market_id -> {"model": "provider:model" | None, "fallback": bool} of the latest web evidence round of a sonnet-arm market.
# Kept apart from _used: the web round runs before the judges, and :func:`reset` (start of the judges) must not clear it.
_web: dict[str, dict[str, Any]] = {}


def arm_for(market_id: str) -> str:
    return evidence.judge_model_arm(market_id)


def reset(market_id: str) -> None:
    with _lock:
        _used.pop(str(market_id), None)


def override_for_arm(arm: str) -> str | None:
    """``provider:model`` the arm's judge calls are routed to (None = the models.yaml judge)."""
    if arm == evidence.JUDGE_MODEL_DEEPSEEK:
        return config.judge_model_alt()
    if arm == evidence.JUDGE_MODEL_SONNET:
        return config.judge_model_sonnet()
    return None


def note_web(market_id: str, model: str | None, fallback: bool) -> None:
    """Record the web evidence round of a sonnet-arm market: the model that searched (None = the normal path) and whether it fell back."""
    with _lock:
        prev = (_web.get(str(market_id)) or {}).get("model")
        # A later fallback round never erases that an earlier round of this market was searched by the model.
        _web[str(market_id)] = {"model": model or (prev if fallback else None), "fallback": bool(fallback)}


def reset_web(market_id: str) -> None:
    """Forget the web round notes of a market (start of a new evidence build)."""
    with _lock:
        _web.pop(str(market_id), None)


def web_fell_back(market_id: str) -> bool:
    with _lock:
        return bool((_web.get(str(market_id)) or {}).get("fallback"))


def web_model(market_id: str) -> str | None:
    """``provider:model`` that actually ran the web search of this market's latest round (None = none / not the sonnet model)."""
    from orchestrator.config import RUNNER_MODE_LIVE, runner_mode  # noqa: PLC0415

    if runner_mode() != RUNNER_MODE_LIVE:
        return None
    with _lock:
        return (_web.get(str(market_id)) or {}).get("model")


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
        override = override_for_arm(arm_for(market_id))
        if override and not fallback:
            spec = routing.parse_model_spec(override)
        else:
            spec = routing.spec_for(ROLE)
        return spec.provider, spec.model
    except Exception:  # noqa: BLE001 - never break a ledger write
        return None, None


def ledger_fields(market_id: str, fallback: bool | None = None) -> dict[str, Any]:
    """``judge_model_arm`` plus (live) the judge provider/model and the web-search model actually used, for ledger rows and the research frontmatter."""
    out: dict[str, Any] = {"judge_model_arm": arm_for(market_id)}
    out["provider"], out["model"] = provider_model(market_id, fallback)
    out["web_model"] = web_model(market_id)
    return out


def log_arm(market_id: str) -> None:
    provider, model = provider_model(market_id)
    label = f"{provider}:{model}" if provider else "stub"
    log.info("[PHASE 3] judge model arm for %s: %s (%s)", market_id, arm_for(market_id), label)
