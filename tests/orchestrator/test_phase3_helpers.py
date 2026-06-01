"""Shared stubs for Phase 3 iterative pipeline tests."""

from __future__ import annotations

from typing import Any

from orchestrator.runner import _stub_deep_researcher_markdown


def briefer_ok(payload: dict[str, Any]) -> dict[str, Any]:
    title = payload.get("market_title", "")
    return {
        "market_id": payload["market_id"],
        "research_queries": [f"stub query for {title}".strip()],
        "error": None,
    }


def deep_researcher_complete(
    payload: dict[str, Any],
    *,
    estimated_p: float = 0.5,
) -> dict[str, Any]:
    market_id = payload["market_id"]
    return {
        "status": "complete",
        "market_id": market_id,
        "estimated_p": estimated_p,
        "markdown": _stub_deep_researcher_markdown(market_id, estimated_p=estimated_p),
    }


def deep_researcher_needs_more(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "needs_more_data",
        "new_queries": ["follow-up stub query"],
    }
