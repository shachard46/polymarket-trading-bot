"""Phase 3 FULL stage: crux pipeline (scout -> architect -> search -> gap -> audit) -> two judges -> Hub decision -> report.

The question-led pipeline itself lives in :mod:`orchestrator.phases.p3_research`; this module wires it into the
per-market flow (evidence reuse, no_evidence skip, aggregation, skips, report, ledger/research state).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from obsidian_utils import ObsidianManager
from orchestrator import compound as compound_mod
from orchestrator.confidence import compute_confidence
from orchestrator import evidence, judge_model, scraper
from orchestrator.directives import extract_filter_directives
from orchestrator.live_prompt import today_date
from orchestrator.phases import p3_research
from orchestrator.phases.common import (
    _as_float,
    _ledger_market_fields,
    _ledger_safe,
    log,
)
from orchestrator.phases.p3_evidence import _bundle_has_entries
from orchestrator.phases.research_state import _record_attempt, _skip_research
from orchestrator.phases.p3_samples import (  # noqa: F401  (re-exported; split out of this module)
    _CONFIDENCE_RANK,
    Sample,
    ShadowFailure,
    _flag,
    aggregate_samples,
)
from orchestrator.runner import AgentRunner
from orchestrator.state import InfraUnavailable, vault_write_or_flag


def _discounted_sources(samples: list[Sample]) -> list[str]:
    """Sources the judges said they discounted (interested / unproven), deduplicated, short."""
    out: list[str] = []
    for s in samples:
        for d in s[0].discounted_sources:
            d = str(d).strip()[:120]
            if d and d not in out:
                out.append(d)
    return out[:6]


def _confidence_fields(
    crux_meta: dict[str, Any] | None,
    fields: dict[str, Any],
    agg: dict[str, Any],
    n_samples: int,
    ev_fallback: bool,
) -> dict[str, Any]:
    """The recorded confidence signals: code level + rule hits (drives sizing) and the judge score / reason.

    Never reads q. The judge fields are record-only unless SIZING_CONFIDENCE_SIGNAL is "judge_score".
    """
    level = compute_confidence(
        crux_meta, fields.get("source_mix"), fields.get("prior_kind"), fields.get("base_rate_n"),
        agg["spread"] if n_samples == 2 else None, fields.get("audit_dropped"), ev_fallback,
    )
    reasons = agg.get("confidence_reasons") or []
    return {
        **level,
        "judge_confidence_score": agg.get("confidence_score"),
        "judge_confidence_reason": " | ".join(reasons)[:400] if reasons else None,
    }


def thin_evidence_disagreement(
    crux_meta: dict[str, Any] | None, p: float, q: float | None, max_gap: float
) -> bool:
    """True when an ``evidence`` crux is still unanswered after the gap round's reassess and |p - q| > ``max_gap``.

    A big gap to the market with the "what is the latest verified state" question unanswered is a probable
    misread of thin information, not an edge (generalised ``probable_misread``; code decides, the judge never sees q).
    """
    if q is None or abs(p - q) <= max_gap:
        return False
    return any(
        c.get("role") == "evidence" and c.get("status") == "unanswered" for c in (crux_meta or {}).get("cruxes") or []
    )


def _prepare_evidence(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    today: str,
    refresh: bool,
    shadow: bool = False,
) -> bool:
    """Ensure the market has an evidence bundle (always ``True``: helper-role failures degrade, never flag).

    An existing usable bundle of THIS market is reused; otherwise the crux evidence stage runs. Never a sibling's
    bundle: the crux plan and bundle are per market (a same-event sibling's cruxes are framed for another outcome).
    """
    market_id = row["market_id"]
    if refresh or not _bundle_has_entries(vault.read_research_bundle(market_id)):
        depth = p3_research.research_depth(row.get("market_data") or {})
        log.info("[PHASE 3] RESEARCH_DEPTH=%s for %s", depth, market_id)
        p3_research.build_evidence(vault, runner, row, today, depth)
    return True


def _collect_samples(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    directives: str,
    today: str,
    shadow: bool = False,
) -> list[Sample] | None:
    """The two judge samples (J1 P(YES), J2 P(NO)); one failed judge is tolerated, both flag the market."""
    market_id = row["market_id"]
    crux_meta = vault.read_research_bundle_meta(market_id).get("crux_meta")
    samples, failed = p3_research.run_judges(vault, runner, row, directives, today, crux_meta)
    if not samples:
        _flag(
            vault, market_id, "phase3", "both deep researcher judges failed", {"failed": failed}, shadow,
        )
        return None
    return samples


def _write_research_report(
    vault: ObsidianManager,
    market_id: str,
    md: dict[str, Any],
    q: float | None,
    agg: dict[str, Any],
    samples: list[Sample],
    ev_source: str,
    ev_fallback: bool,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Persist the chosen sample's report to ``02_Active_Research/``."""
    chosen, chosen_research = samples[agg["chosen"]][:2]
    payload = {
        "market_id": market_id,
        "estimated_p": agg["p"],
        "error": None,
        "p_low": agg["p_low"],
        "p_high": agg["p_high"],
        "confidence": agg["confidence"],
        "as_of": chosen.as_of.isoformat(),
        "resolution_checklist": chosen.resolution_checklist,
        "key_assumptions": chosen.key_assumptions,
        "q_at_research": q,  # Hub-side only (refresh trigger); never sent to a model
        "researched_at": datetime.now(timezone.utc).isoformat(),
        "end_date": md.get("end_date"),
        "days_at_research": _as_float(md.get("days_to_resolution")),
        "samples": agg["samples"],
        "evidence_source": ev_source,
        "evidence_fallback": ev_fallback,
        **(extra or {}),
    }
    return vault_write_or_flag(
        vault=vault,
        market_id=market_id,
        write_fn=lambda: vault.write_research_report(market_id, payload, chosen_research.body),
        payload=payload,
        artifact_label="research report",
        phase="phase3",
    )


def _research_market(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    directives: str,
    *,
    refresh: bool = False,
    shadow: bool = False,
    quick: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """FULL stage for one market: crux evidence pipeline -> two judges (J1 YES, J2 NO) -> Hub decision.

    ``shadow`` (measure only, a quick no_divergence skip): runs the same research but writes no
    report and leaves ``research_state`` untouched; one ``shadow_research`` ledger row, returns None.

    Returns the Phase 4 row (``p_value``/``p_low``/``p_high``/``market_data``) when active
    research was written, ``None`` after a ledgered skip or an inactive flag.
    """
    if not shadow:
        return _research_market_body(
            vault, runner, row, directives, refresh, False, quick
        )
    market_id = row["market_id"]
    try:
        return _research_market_body(
            vault, runner, row, directives, refresh, True, quick
        )
    except InfraUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - a shadow run must never flag the real market
        if isinstance(exc, ShadowFailure):
            reason, detail = exc.reason, exc.payload
        else:
            reason, detail = f"{type(exc).__name__}: {exc}", None
        _ledger_safe(
            vault, market_id, "shadow_failed",
            shadow=True, reason=reason[:500], error=str(detail)[:1000] if detail else str(exc)[:1000],
            **_ledger_market_fields(row.get("market_data"), role="deep_researcher", market_id=market_id),
        )
        log.warning("[PHASE 3] shadow_failed for %s: %s", market_id, reason)
        return None


def _research_market_body(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    directives: str,
    refresh: bool,
    shadow: bool,
    quick: dict[str, Any] | None,
) -> dict[str, Any] | None:
    market_id = row["market_id"]
    md = row.get("market_data") or {}
    q = scraper.select_q(md)
    today = today_date()

    if not _prepare_evidence(
        vault, runner, row, today, refresh, shadow
    ):
        return None
    if not _bundle_has_entries(vault.read_research_bundle(market_id)):
        # Never send an empty / NOT_FOUND-only bundle to the Deep Researcher.
        ev_meta = vault.read_research_bundle_meta(market_id)
        if shadow:
            raise ShadowFailure("no_evidence")
        _skip_research(
            vault, row, "full", "no_evidence", q=q,
            evidence_source=ev_meta.get("evidence_source"),
            evidence_fallback=bool(ev_meta.get("evidence_fallback")),
        )
        return None
    samples = _collect_samples(vault, runner, row, directives, today, shadow)
    if samples is None:
        return None

    agg = aggregate_samples(samples)
    ev_meta = vault.read_research_bundle_meta(market_id)
    ev_source = ev_meta.get("evidence_source") or evidence.AIQ  # legacy bundles were A-IQ
    ev_fallback = bool(ev_meta.get("evidence_fallback"))
    failed = p3_research.failed_judges(market_id, samples)
    arm, plan = p3_research.judge_plan(market_id)
    crux_meta = ev_meta.get("crux_meta")
    spec = (crux_meta or {}).get("compound")
    comp = compound_mod.check(samples, spec) if spec else None
    fields = p3_research.crux_fields(
        crux_meta, agg["spread"] if len(samples) == 2 else None, failed,
        judge_arm=arm, judge_framing=plan[0][1] if arm == "one" else None, compound=comp,
    )
    discounted = _discounted_sources(samples)
    if discounted:
        fields["discounted_sources"] = discounted
    pf = p3_research.prior_fields(samples)
    pflags = pf.pop("prior_flags", [])
    fields.update(pf)
    fields.update(_confidence_fields(crux_meta, fields, agg, len(samples), ev_fallback))
    if pflags:
        fields["pipeline_flags"] = list(fields.get("pipeline_flags") or []) + [f"prior:{f}" for f in pflags]
    if judge_model.fell_back(market_id):  # the alt judge model failed: the primary answered; keeps the arm measurable
        fields["pipeline_flags"] = list(fields.get("pipeline_flags") or []) + [judge_model.FALLBACK_FLAG]
    if comp and comp["compound_gap"] is not None:
        log.info(
            "[PHASE 3] compound %s for %s: combined=%.3f stated=%.3f gap=%.3f corr=%.2f",
            comp["compound_type"], market_id, comp["compound_p"], comp["compound_stated_p"],
            comp["compound_gap"], comp["compound_corr"],
        )
    knobs = extract_filter_directives(directives)
    compound_bad = bool(
        comp and comp["compound_gap"] is not None and comp["compound_gap"] > knobs["research_max_sample_spread"]
    )
    values = {
        "q": q, "p": agg["p"], "p_low": agg["p_low"], "p_high": agg["p_high"],
        "samples": agg["samples"], "spread": agg["spread"], "confidence": agg["confidence"],
        "evidence_source": ev_source, "evidence_fallback": ev_fallback, **fields,
    }
    if shadow:
        would_skip = None
        if agg["spread"] > knobs["research_max_sample_spread"]:
            would_skip = "sample_disagreement"
        elif compound_bad:
            would_skip = "compound_inconsistent"
        elif q is not None and abs(agg["p"] - q) > knobs["research_max_q_gap"]:
            would_skip = "probable_misread"
        elif thin_evidence_disagreement(crux_meta, agg["p"], q, knobs["research_thin_evidence_max_gap"]):
            would_skip = "thin_evidence_disagreement"
        quick = quick or {}
        _ledger_safe(
            vault, market_id, "shadow_research",
            shadow=True, would_skip=would_skip,
            quick_p=quick.get("p"), quick_p_low=quick.get("p_low"), quick_p_high=quick.get("p_high"),
            **values, **_ledger_market_fields(md, role="deep_researcher", market_id=market_id),
        )
        log.info("[PHASE 3] shadow_research for %s (would_skip=%s)", market_id, would_skip)
        return None
    if agg["spread"] > knobs["research_max_sample_spread"]:
        _skip_research(vault, row, "full", "sample_disagreement", **values)
        return None
    if compound_bad:
        _skip_research(vault, row, "full", "compound_inconsistent", **values)
        return None
    if q is not None and abs(agg["p"] - q) > knobs["research_max_q_gap"]:
        _skip_research(vault, row, "full", "probable_misread", **values)
        return None
    if thin_evidence_disagreement(crux_meta, agg["p"], q, knobs["research_thin_evidence_max_gap"]):
        log.info(
            "[PHASE 3] thin_evidence_disagreement for %s: p=%.3f q=%.3f, an evidence crux is unanswered",
            market_id, agg["p"], q,
        )
        _skip_research(vault, row, "full", "thin_evidence_disagreement", **values)
        return None

    offer_ev = p3_research.offer_evidence(vault.read_research_bundle(market_id) or [], today, samples)
    if not _write_research_report(
        vault, market_id, md, q, agg, samples, ev_source, ev_fallback,
        {**fields, **offer_ev, **judge_model.ledger_fields(market_id)},
    ):
        return None
    _record_attempt(vault, row, "full", "researched", **values)
    return {
        "market_id": market_id,
        "p_value": agg["p"],
        "p_low": agg["p_low"],
        "p_high": agg["p_high"],
        "market_data": md,
        "evidence_source": ev_source,
        "evidence_fallback": ev_fallback,
        **fields,
    }
