"""Phase 3 QUICK stage: one price-blind Deep Researcher call, then the Hub escalate/skip decision."""

from __future__ import annotations

from typing import Any, Literal, NamedTuple

from pydantic import ValidationError

from obsidian_utils import ObsidianManager
from config.trading_constants import RESEARCH_MAX_QUICK_WIDTH
from orchestrator import scraper
from orchestrator.agent_outputs import DeepResearcherNeedsMore
from orchestrator.config import FORCED_SYNTHESIS_OVERRIDE
from orchestrator.directives import extract_filter_directives
from orchestrator.phases.agent_call import check_complete, invoke_deep_researcher
from orchestrator.phases.common import _dr_payload
from orchestrator.phases.research_state import _record_attempt, _skip_research
from orchestrator.runner import AgentRunner
from orchestrator.state import flag_inactive


class QuickResult(NamedTuple):
    outcome: Literal["escalate", "skip", "failed"]
    p: float | None = None
    p_low: float | None = None
    p_high: float | None = None


def quick_decision(
    p_low: float, p_high: float, q: float, max_width: float | None = None
) -> Literal["no_divergence", "escalate"]:
    """Hub decision after the QUICK stage: skip only when q sits inside a narrow range.

    ``max_width`` is the directives knob ``research_max_quick_width`` (default constant).
    """
    inside = p_low <= q <= p_high
    width = RESEARCH_MAX_QUICK_WIDTH if max_width is None else max_width
    if inside and (p_high - p_low) <= width:
        return "no_divergence"
    return "escalate"


def _quick_stage(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    directives: str,
    today: str,
) -> QuickResult:
    """One price-blind Deep Researcher call with no A-IQ bundle, then the Hub decision.

    Returns the outcome plus the quick p/p_low/p_high (None when it failed before an estimate).
    """
    market_id = row["market_id"]
    md = row.get("market_data") or {}
    q = scraper.select_q(md)
    dr_in = _dr_payload(
        row, directives, [], mode="quick", today=today,
        system_override=FORCED_SYNTHESIS_OVERRIDE, session_tag="quick",
    )
    try:
        out = invoke_deep_researcher(runner, dr_in)
    except ValidationError as exc:
        flag_inactive(
            vault, market_id, "phase3",
            f"deep researcher quick-stage output validation failed: {exc}", dr_in,
        )
        return QuickResult("failed")
    if isinstance(out, DeepResearcherNeedsMore):
        flag_inactive(
            vault, market_id, "phase3",
            "deep researcher disobeyed quick-stage override", out.model_dump(),
        )
        return QuickResult("failed")
    if check_complete(vault, market_id, out) is None:
        return QuickResult("failed")

    values = {
        "q": q, "p": out.estimated_p, "p_low": out.p_low, "p_high": out.p_high,
        "evidence_source": "none",  # the quick stage sees no evidence bundle
    }
    knobs = extract_filter_directives(directives)
    if quick_decision(out.p_low, out.p_high, q, knobs["research_max_quick_width"]) == "no_divergence":
        _skip_research(vault, row, "quick", "no_divergence", **values)
        return QuickResult("skip", out.estimated_p, out.p_low, out.p_high)
    _record_attempt(vault, row, "quick", "escalate", **values)
    return QuickResult("escalate", out.estimated_p, out.p_low, out.p_high)
