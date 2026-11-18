"""Phase 3 FULL stage: sample helpers (extra/independent samples, collection, aggregation)."""

from __future__ import annotations

import statistics
from typing import Any, Callable

from pydantic import ValidationError

from obsidian_utils import ObsidianManager
from config.trading_constants import RESEARCH_SAMPLES
from orchestrator import evidence
from orchestrator.agent_outputs import DeepResearcherComplete, DeepResearcherNeedsMore
from orchestrator.config import FORCED_SYNTHESIS_OVERRIDE
from orchestrator.phases.agent_call import check_complete, invoke_deep_researcher
from orchestrator.phases.common import _dr_payload, log
from orchestrator.phases.p3_evidence import fetch_other_source_bundle
from orchestrator.runner import AgentRunner
from orchestrator.state import InfraUnavailable, flag_inactive

_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

Sample = tuple[DeepResearcherComplete, Any]


class ShadowFailure(Exception):
    """A content/agent error inside a shadow FULL run (ledgered as ``shadow_failed``)."""

    def __init__(self, reason: str, payload: Any = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.payload = payload


def _flag(
    vault: ObsidianManager,
    market_id: str,
    phase: str,
    reason: str,
    payload: Any,
    shadow: bool,
) -> None:
    """Flag the market inactive; a shadow run raises :class:`ShadowFailure` instead (never flags)."""
    if shadow:
        raise ShadowFailure(reason, payload)
    flag_inactive(vault, market_id, phase, reason, payload)


def aggregate_samples(
    samples: list[tuple[DeepResearcherComplete, Any]],
) -> dict[str, Any]:
    """Combine independent FULL-stage samples.

    ``p``, ``p_low`` and ``p_high`` are each the **median** across samples (min/max
    would let one outlier widen the range and are dominated by sample noise; the
    spread guard already rejects disagreement). Medians of ordered triples keep
    ``p_low <= p <= p_high``. ``chosen`` is the sample closest to the median ``p``
    (its markdown body is the one written); ``confidence`` is the most conservative.
    """
    ps = [c.estimated_p for c, _ in samples]
    p = statistics.median(ps)
    chosen = min(range(len(ps)), key=lambda i: (abs(ps[i] - p), i))
    confidence = min((c.confidence for c, _ in samples), key=lambda c: _CONFIDENCE_RANK[c])
    return {
        "p": p,
        "p_low": statistics.median(c.p_low for c, _ in samples),
        "p_high": statistics.median(c.p_high for c, _ in samples),
        "samples": ps,
        "spread": max(ps) - min(ps),
        "confidence": confidence,
        "chosen": chosen,
    }


def _extra_sample(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    directives: str,
    today: str,
    bundle: list[dict[str, Any]],
    tag: str,
) -> tuple[DeepResearcherComplete, Any] | None:
    """Independent re-estimate on the lead sample's final bundle (own session, no fetches)."""
    market_id = row["market_id"]
    dr_in = _dr_payload(
        row, directives, bundle, mode="full", today=today,
        system_override=FORCED_SYNTHESIS_OVERRIDE, session_tag=tag,
    )
    try:
        out = invoke_deep_researcher(runner, dr_in)
    except ValidationError as exc:
        log.warning("[PHASE 3] sample %s for %s failed validation: %s", tag, market_id, exc)
        return None
    if isinstance(out, DeepResearcherNeedsMore):
        log.warning("[PHASE 3] sample %s for %s ignored the override", tag, market_id)
        return None
    research = check_complete(vault, market_id, out, flag=False)
    return (out, research) if research is not None else None


def _indep_sample(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    directives: str,
    today: str,
    ev_source: str,
    p_median: float,
) -> dict[str, Any]:
    """Measure-only extra sample on the OTHER evidence source (same queries). Never flags.

    Returns ``indep_*`` fields, or ``{"indep_error": ...}``. Not part of median/spread/skip/sizing.
    """
    market_id = row["market_id"]
    try:
        queries = list(dict.fromkeys(
            str(e.get("query") or "").strip()
            for e in vault.read_research_bundle(market_id) or []
            if str(e.get("query") or "").strip()
        ))
        if not queries:
            return {"indep_error": "no queries in bundle"}
        source, rows = fetch_other_source_bundle(vault, market_id, queries, ev_source, runner, today)
        if not evidence.rows_usable(rows):
            return {"indep_error": f"no usable {source} evidence", "indep_source": source}
        got = _extra_sample(vault, runner, row, directives, today, rows, "indep")
        if got is None:
            return {"indep_error": "sample invalid", "indep_source": source}
        out = got[0]
        return {
            "indep_p": out.estimated_p,
            "indep_p_low": out.p_low,
            "indep_p_high": out.p_high,
            "indep_source": source,
            "indep_gap": out.estimated_p - p_median,
        }
    except InfraUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - measure-only; must never flag the market
        log.warning("[PHASE 3] indep sample for %s failed: %s", market_id, exc)
        return {"indep_error": f"{type(exc).__name__}: {exc}"[:300]}


def _collect_samples(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    directives: str,
    today: str,
    shadow: bool = False,
    *,
    research_loop: Callable[..., DeepResearcherComplete | None],
) -> list[Sample] | None:
    """Lead sample plus independent extra samples; ``None`` after an inactive flag.

    ``research_loop`` is the lead-sample runner (kept in :mod:`p3_full`, passed in to avoid a cycle).
    """
    market_id = row["market_id"]
    lead = research_loop(vault, runner, row, directives, today, "s1", shadow)
    if lead is None:
        return None
    lead_research = check_complete(
        vault, market_id, lead,
        on_fail=(lambda reason, payload: _flag(vault, market_id, "phase3", reason, payload, True))
        if shadow else None,
    )
    if lead_research is None:
        return None
    samples: list[Sample] = [(lead, lead_research)]

    bundle = vault.read_research_bundle(market_id) or []
    for k in range(2, RESEARCH_SAMPLES + 1):
        extra = _extra_sample(vault, runner, row, directives, today, bundle, f"s{k}")
        if extra is not None:
            samples.append(extra)
    if len(samples) < 2:
        _flag(
            vault, market_id, "phase3",
            "fewer than 2 valid deep researcher samples", {"valid": len(samples)}, shadow,
        )
        return None
    return samples
