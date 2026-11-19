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
from orchestrator.phases.common import log, _dr_payload, _ledger_market_fields, _ledger_safe
from orchestrator.phases.research_state import _record_attempt, _skip_research
from orchestrator.runner import AgentRunner
from orchestrator.schema_validation import AgentSchemaError
from orchestrator.state import flag_inactive


class QuickResult(NamedTuple):
    outcome: Literal["escalate", "skip", "failed"]
    p: float | None = None
    p_low: float | None = None
    p_high: float | None = None


def quick_decision(
    p_low: float, p_high: float, q: float, max_width: float | None = None, confidence: str | None = None
) -> Literal["no_divergence", "escalate"]:
    """Hub decision after the QUICK stage: skip only when q sits inside a narrow range.

    ``max_width`` is the directives knob ``research_max_quick_width`` (default constant). A ``"low"``
    confidence quick result never skips: the model says its knowledge is thin.
    """
    if confidence == "low":
        return "escalate"
    inside = p_low <= q <= p_high
    width = RESEARCH_MAX_QUICK_WIDTH if max_width is None else max_width
    if inside and (p_high - p_low) <= width:
        return "no_divergence"
    return "escalate"


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def _log_quick(market_id: str, out: Any, q: float | None, result: str) -> None:
    """One INFO line per market: the quick estimate, its range, the market price and the Hub decision."""
    if out is None:
        log.info("[QUICK] %s: p=n/a range=n/a q=%s → %s", market_id[:10], _fmt(q), result)
        return
    log.info(
        "[QUICK] %s: p=%s range=%s-%s q=%s → %s",
        market_id[:10], _fmt(out.estimated_p), _fmt(out.p_low), _fmt(out.p_high), _fmt(q), result,
    )


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
        _log_quick(market_id, None, q, "failed:output_validation")
        return QuickResult("failed")
    if isinstance(out, DeepResearcherNeedsMore):
        flag_inactive(
            vault, market_id, "phase3",
            "deep researcher disobeyed quick-stage override", out.model_dump(),
        )
        _log_quick(market_id, None, q, "failed:ignored_override")
        return QuickResult("failed")
    if check_complete(vault, market_id, out) is None:
        _log_quick(market_id, None, q, "failed:incomplete_output")
        return QuickResult("failed")

    values = {
        "q": q, "p": out.estimated_p, "p_low": out.p_low, "p_high": out.p_high,
        "evidence_source": "none",  # the quick stage sees no evidence bundle
    }
    knobs = extract_filter_directives(directives)
    if quick_decision(out.p_low, out.p_high, q, knobs["research_max_quick_width"], out.confidence) == "no_divergence":
        _skip_research(vault, row, "quick", "no_divergence", **values)
        _log_quick(market_id, out, q, "skip:no_divergence")
        return QuickResult("skip", out.estimated_p, out.p_low, out.p_high)
    _record_attempt(vault, row, "quick", "escalate", **values)
    _log_quick(market_id, out, q, "escalate_full")
    return QuickResult("escalate", out.estimated_p, out.p_low, out.p_high)


def _triage_shadow_payload(row: dict[str, Any], directives: str, today: str) -> dict[str, Any]:
    """The price-blind quick-stage Deep Researcher payload of a triage shadow."""
    return _dr_payload(
        row, directives, [], mode="quick", today=today,
        system_override=FORCED_SYNTHESIS_OVERRIDE, session_tag="quick",
    )


def _triage_shadow_quick(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    directives: str,
    today: str,
    triage: dict[str, Any],
) -> None:
    """Measure-only quick estimate for a triage reject: one price-blind call, one ledger row.

    Writes ONE `triage_shadow` row (q, p, p_low, p_high, triage reason, manipulation_risk, market
    fields). Never escalates, trades, changes status or flags the market inactive: a content error
    is ledgered in the `error` field. Infra errors propagate (the caller leaves it pending).
    """
    market_id = row["market_id"]
    md = row.get("market_data") or {}
    q = scraper.select_q(md)
    high = triage.get("manipulation_risk") == "high"
    fields = {
        **_ledger_market_fields(md, role="deep_researcher"),
        "reason": "manipulation_risk" if high else triage.get("reason"),
        "triage_kind": "manipulation_risk" if high else "not_researchable",
        "q": q,
    }
    if fields.get("category") is None:
        fields["category"] = triage.get("category")
    dr_in = _triage_shadow_payload(row, directives, today)
    error: str | None = None
    out: Any = None
    try:
        out = invoke_deep_researcher(runner, dr_in)
    except (ValueError, AgentSchemaError) as exc:  # content errors (pydantic/parse); infra propagates
        error = f"quick-stage output invalid: {exc}"
    if error is None and isinstance(out, DeepResearcherNeedsMore):
        error = "deep researcher disobeyed quick-stage override"
    if error is None and check_complete(vault, market_id, out, flag=False) is None:
        error = "deep researcher quick-stage output incomplete or inconsistent"
    if error is not None:
        log.warning("[PHASE 3] triage_shadow for %s failed: %s", market_id, error)
        _ledger_safe(vault, market_id, "triage_shadow", error=error, **fields)
        return
    _ledger_safe(
        vault, market_id, "triage_shadow",
        p=out.estimated_p, p_low=out.p_low, p_high=out.p_high, **fields,
    )
