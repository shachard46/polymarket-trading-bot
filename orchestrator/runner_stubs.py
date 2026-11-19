"""Deterministic, schema-valid stub responses (success path) keyed by agent role.

Used by the ``stub`` and ``stub_error`` runner modes (see :mod:`orchestrator.runner`).
"""

from __future__ import annotations

from typing import Any, Callable

StubBuilder = Callable[[dict[str, Any]], Any]


def _triage_stub(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_id": payload.get("market_id", "stub"),
        "researchable": True,
        "category": "stub",
        "reason": "stub: researchable",
        "manipulation_risk": "low",
        "manipulation_reason": "stub",
        "error": None,
    }


# Stub estimate: far enough from a q of ~0.26 to escalate, pass the misread guard and
# clear the shrunk, tax-aware Kelly edge (so the stub pipeline reaches a paper trade),
# yet inside the range for a q of ~0.55 (quick-stage ``no_divergence``).
STUB_ESTIMATED_P = 0.55
STUB_P_LOW = 0.51
STUB_P_HIGH = 0.59


def _stub_deep_researcher_markdown(
    market_id: str,
    *,
    estimated_p: float = STUB_ESTIMATED_P,
    p_low: float | None = None,
    p_high: float | None = None,
    error: str | None = None,
) -> str:
    err_yaml = "null" if error is None else f'"{error}"'
    p_low = max(0.0, estimated_p - 0.08) if p_low is None else p_low
    p_high = min(1.0, estimated_p + 0.08) if p_high is None else p_high
    return (
        "---\n"
        f'market_id: "{market_id}"\n'
        f"estimated_p: {estimated_p}\n"
        f"p_low: {p_low}\n"
        f"p_high: {p_high}\n"
        f"error: {err_yaml}\n"
        "---\n\n"
        "## Bull Thesis\n\n(stub bull thesis)\n\n"
        "## Bear Thesis\n\n(stub bear thesis)\n\n"
        "## Post-Mortem\n"
    )


def _briefer_stub(payload: dict[str, Any]) -> dict[str, Any]:
    title = str(payload.get("title", "")).strip()
    today = payload.get("today", "")
    slots = (
        "resolution criteria and polarity",
        "reference class base rate",
        "current state",
        "strongest disconfirming evidence",
    )
    return {
        "market_id": payload.get("market_id", "stub"),
        "research_queries": [f"stub {slot} for {title} as of {today}" for slot in slots],
        "error": None,
    }


def _web_searcher_stub(payload: dict[str, Any]) -> dict[str, Any]:
    today = payload.get("today", "")
    return {
        "market_id": payload.get("market_id", "stub"),
        "results": [
            {
                "query": q,
                "research_data": f"- stub web finding for: {q} | https://example.org/stub | {today or 'unknown'}",
                "error": None,
            }
            for q in payload.get("queries") or []
        ],
        "error": None,
    }


def _web_extractor_stub(payload: dict[str, Any]) -> dict[str, Any]:
    today = payload.get("today", "")
    pages = payload.get("pages") or []
    url = (pages[0].get("url") if pages and isinstance(pages[0], dict) else None) or "https://example.org/stub"
    q = payload.get("query", "")
    return {
        "market_id": payload.get("market_id", "stub"),
        "results": [
            {
                "query": q,
                "research_data": f"- stub extracted finding for: {q} | {url} | {today or 'unknown'}",
                "error": None,
            }
        ],
        "error": None,
    }


def _deep_researcher_stub(payload: dict[str, Any]) -> dict[str, Any]:
    market_id = payload.get("market_id", "stub")
    today = payload.get("today") or "2026-01-01"
    return {
        "status": "complete",
        "market_id": market_id,
        "estimated_p": STUB_ESTIMATED_P,
        "p_low": STUB_P_LOW,
        "p_high": STUB_P_HIGH,
        "confidence": "medium",
        "resolution_checklist": ["stub: YES requires the titled event"],
        "key_assumptions": ["stub assumption"],
        "as_of": today,
        "markdown": _stub_deep_researcher_markdown(
            market_id, p_low=STUB_P_LOW, p_high=STUB_P_HIGH
        ),
    }


def _post_mortem_stub(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_id": payload.get("market_id", "stub"),
        "post_mortem_analysis": "stub post-mortem analysis paragraph.",
        "failure_class": "variance",
        "failure_tag_definition": None,
        "error": None,
    }


def _overseer_stub(payload: dict[str, Any]) -> dict[str, Any]:
    # Re-emit the existing directives so the structural validator still passes.
    current = payload.get("current_directives") or ""
    return {
        "new_directives_markdown": current
        or (
            "---\n"
            "title: stub\n"
            "---\n\n"
            "## Research Protocol\n\nstub.\n\n"
            "## Filter Weightings\n\nstub.\n\n"
            "```yaml\nfilters:\n  gate_min_q: 0.05\n```\n\n"
            "## Risk Constraints\n\nstub.\n\n"
            "## Output Requirements\n\nstub.\n"
        ),
        "rationale": "stub: no changes recommended",
        "error": None,
    }


STUB_RESPONSES: dict[str, StubBuilder] = {
    "triage": _triage_stub,
    "briefer": _briefer_stub,
    "web_searcher": _web_searcher_stub,
    "web_extractor": _web_extractor_stub,
    "deep_researcher": _deep_researcher_stub,
    "post_mortem_analyst": _post_mortem_stub,
    "overseer": _overseer_stub,
}
