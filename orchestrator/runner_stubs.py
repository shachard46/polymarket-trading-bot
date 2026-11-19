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


def _scout_stub(payload: dict[str, Any]) -> dict[str, Any]:
    title = str(payload.get("title", "")).strip()
    return {
        "market_id": payload.get("market_id", "stub"),
        "event": f"stub landscape for {title}",
        "deciding_actors": ["stub actor"],
        "resolution_source": payload.get("resolution_source"),
        "scheduled": [],
        "contested": [],
        "main_sources": ["example.org"],
        "error": None,
    }


def _briefer_stub(payload: dict[str, Any]) -> dict[str, Any]:
    title = str(payload.get("title", "")).strip()
    today = payload.get("today", "")
    slots = (
        ("mechanism", "how and when it resolves"),
        ("comparator", "reference class base rate"),
        ("evidence", "current state"),
        ("horizon", "catalysts before the end date"),
        ("skeptic", "strongest case against"),
    )
    if payload.get("depth") == "lite":
        slots = slots[:4]
    return {
        "market_id": payload.get("market_id", "stub"),
        "cruxes": [
            {
                "role": role,
                "question": f"stub {what} for {title}?",
                "queries": [f"stub {what} for {title} as of {today}"],
                "moves_p_if": "stub: a yes answer pushes p up",
            }
            for role, what in slots
        ],
        "error": None,
    }


def _gap_analyzer_stub(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_id": payload.get("market_id", "stub"),
        "assessments": [
            {"crux_id": c.get("crux_id"), "status": "answered", "missing": ""}
            for c in payload.get("cruxes") or []
        ],
        "follow_up_queries": [],
        "error": None,
    }


def _auditor_stub(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_id": payload.get("market_id", "stub"),
        "verdicts": [
            {"id": it.get("id"), "supported": True, "reason": "stub: supported"}
            for it in payload.get("items") or []
        ],
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
    p, low, high = STUB_ESTIMATED_P, STUB_P_LOW, STUB_P_HIGH
    if payload.get("ask") == "no":  # judge J2: the probability of NO (the complement of the YES stub)
        p, low, high = round(1 - p, 6), round(1 - STUB_P_HIGH, 6), round(1 - STUB_P_LOW, 6)
    out: dict[str, Any] = {
        "status": "complete",
        "market_id": market_id,
        "estimated_p": p,
        "p_low": low,
        "p_high": high,
        "confidence": "medium",
        "resolution_checklist": ["stub: YES requires the titled event"],
        "key_assumptions": ["stub assumption"],
        "as_of": today,
        "markdown": _stub_deep_researcher_markdown(
            market_id, estimated_p=p, p_low=low, p_high=high
        ),
    }
    if payload.get("mode") == "judge":
        out["base_rate"] = None
        out["crux_verdicts"] = [
            {"crux_id": c.get("crux_id"), "direction": "yes", "cited": list(c.get("evidence_ids") or [])[:1],
             "note": "stub"}
            for c in payload.get("crux_table") or []
        ]
    return out


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
    "scout": _scout_stub,
    "briefer": _briefer_stub,
    "gap_analyzer": _gap_analyzer_stub,
    "auditor": _auditor_stub,
    "web_searcher": _web_searcher_stub,
    "web_extractor": _web_extractor_stub,
    "deep_researcher": _deep_researcher_stub,
    "post_mortem_analyst": _post_mortem_stub,
    "overseer": _overseer_stub,
}
