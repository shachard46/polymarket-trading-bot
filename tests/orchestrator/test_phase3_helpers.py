"""Shared stubs for Phase 3 research pipeline tests."""

from __future__ import annotations

from typing import Any

from orchestrator.runner import _stub_deep_researcher_markdown

# Minimal snapshot so phase 3 pricing fail-fast passes in unit tests.
STUB_MARKET_DATA: dict[str, float] = {
    "yes_price": 0.42,
    "volume": 100.0,
    "liquidity": 200.0,
}

TODAY = "2026-10-22"


def briefer_ok(payload: dict[str, Any]) -> dict[str, Any]:
    title = payload.get("title", "")
    today = payload.get("today", TODAY)
    return {
        "market_id": payload["market_id"],
        "research_queries": [
            f"stub query {slot} for {title} as of {today}".strip() for slot in range(1, 5)
        ],
        "error": None,
    }


def deep_researcher_complete(
    payload: dict[str, Any],
    *,
    estimated_p: float = 0.6,
    p_low: float | None = None,
    p_high: float | None = None,
    confidence: str = "medium",
) -> dict[str, Any]:
    """Price-blind complete payload; the default p=0.6 +/- 0.08 diverges from q=0.42."""
    market_id = payload["market_id"]
    p_low = max(0.0, estimated_p - 0.08) if p_low is None else p_low
    p_high = min(1.0, estimated_p + 0.08) if p_high is None else p_high
    if payload.get("ask") == "no":  # judge J2 is asked P(NO): the complement of the scripted YES estimate
        estimated_p, p_low, p_high = round(1 - estimated_p, 6), round(1 - p_high, 6), round(1 - p_low, 6)
    return {
        "status": "complete",
        "market_id": market_id,
        "estimated_p": estimated_p,
        "p_low": p_low,
        "p_high": p_high,
        "confidence": confidence,
        "resolution_checklist": ["YES requires the titled event"],
        "key_assumptions": ["no surprise ruling"],
        "as_of": TODAY,
        "markdown": _stub_deep_researcher_markdown(
            market_id, estimated_p=estimated_p, p_low=p_low, p_high=p_high
        ),
    }


HELPER_ROLES = ("scout", "gap_analyzer", "auditor")


def helper_role_response(role: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministic stub answer for the crux pipeline's helper roles (what the shipped stub runner returns)."""
    from orchestrator.runner_stubs import STUB_RESPONSES

    return STUB_RESPONSES[role](payload)


def deep_researcher_needs_more(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "needs_more_data",
        "new_queries": ["follow-up stub query"],
    }
