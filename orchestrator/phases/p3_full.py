"""Phase 3 FULL stage: Briefer -> evidence -> Deep Researcher x3 -> Hub decision -> research report."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from obsidian_utils import ObsidianManager
import agents_blueprint  # module import (not from-import) avoids a cycle when agents_blueprint is imported first
from orchestrator import evidence, scraper
from orchestrator.agent_outputs import (
    BrieferOutput,
    DeepResearcherComplete,
    DeepResearcherNeedsMore,
)
from orchestrator.config import FORCED_SYNTHESIS_OVERRIDE, MAX_RESEARCH_ITERATIONS
from orchestrator.directives import extract_filter_directives
from orchestrator.live_prompt import today_date
from orchestrator.parse import (
    agent_error_reason,
    normalize_structured_output,
    run_agent_with_format_retries,
)
from orchestrator.phases.agent_call import invoke_deep_researcher
from orchestrator.phases.common import (
    _as_float,
    _dr_payload,
    _ledger_market_fields,
    _ledger_safe,
    _text_fields,
    log,
)
from orchestrator.phases.p3_evidence import (
    _bundle_has_entries,
    _ensure_as_of,
    _fetch_and_persist_bundle,
)
from orchestrator.phases.research_state import _record_attempt, _skip_research
from orchestrator.phases.p3_samples import (  # noqa: F401  (re-exported; split out of this module)
    _CONFIDENCE_RANK,
    Sample,
    ShadowFailure,
    _extra_sample,
    _flag,
    _indep_sample,
    aggregate_samples,
)
from orchestrator.phases.p3_samples import _collect_samples as _collect_samples_impl
from orchestrator.runner import AgentRunner
from orchestrator.state import InfraUnavailable, vault_write_or_flag

def _plan_research_queries(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    today: str,
    shadow: bool = False,
) -> list[str] | None:
    market_id = row["market_id"]
    brief_in = _text_fields(row, today)
    spec = agents_blueprint.AGENTS["briefer"]

    def _validate_briefer(parsed: dict[str, Any]) -> BrieferOutput:
        normalized = normalize_structured_output(
            "briefer",
            brief_in,
            parsed,
            output_schema=spec.get("output_schema"),
        )
        return BrieferOutput.model_validate(normalized)

    try:
        brief = run_agent_with_format_retries(
            runner, "briefer", brief_in, validate_fn=_validate_briefer
        )
    except ValidationError as exc:
        _flag(
            vault,
            market_id,
            "phase3",
            f"briefer output validation failed: {exc}",
            brief_in,
            shadow,
        )
        return None

    err = agent_error_reason(brief.model_dump())
    if err:
        _flag(vault, market_id, "phase3", f"briefer error: {err}", brief.model_dump(), shadow)
        return None

    return [_ensure_as_of(q, today) for q in brief.research_queries]


def _research_loop(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    directives: str,
    today: str,
    tag: str,
    shadow: bool = False,
) -> DeepResearcherComplete | None:
    """Lead FULL sample: one round, one follow-up round, then forced synthesis."""
    market_id = row["market_id"]
    last_out: DeepResearcherComplete | DeepResearcherNeedsMore | None = None

    for _ in range(MAX_RESEARCH_ITERATIONS):
        dr_in = _dr_payload(
            row, directives, vault.read_research_bundle(market_id) or [],
            mode="full", today=today, session_tag=tag,
        )
        try:
            last_out = invoke_deep_researcher(runner, dr_in)
        except ValidationError as exc:
            _flag(
                vault, market_id, "phase3",
                f"deep researcher output validation failed: {exc}", dr_in, shadow,
            )
            return None
        if isinstance(last_out, DeepResearcherComplete):
            return last_out
        _fetch_and_persist_bundle(
            vault, market_id, list(last_out.new_queries), runner, today
        )

    override_in = _dr_payload(
        row, directives, vault.read_research_bundle(market_id) or [],
        mode="full", today=today, system_override=FORCED_SYNTHESIS_OVERRIDE,
        session_tag=tag,
    )
    try:
        forced = invoke_deep_researcher(runner, override_in)
    except ValidationError as exc:
        _flag(
            vault, market_id, "phase3",
            f"deep researcher forced synthesis validation failed: {exc}", override_in, shadow,
        )
        return None
    if isinstance(forced, DeepResearcherNeedsMore):
        _flag(
            vault, market_id, "phase3",
            "deep researcher disobeyed forced synthesis override", forced.model_dump(), shadow,
        )
        return None
    return forced


def _prepare_evidence(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    today: str,
    refresh: bool,
    shared_bundle: list[dict[str, Any]] | None,
    shared_meta: dict[str, Any] | None,
    shadow: bool = False,
) -> bool:
    """Ensure the market has an evidence bundle; ``False`` after a flagged Briefer failure."""
    market_id = row["market_id"]
    if shared_bundle:  # same-event sibling: reuse the first market's evidence
        vault.clear_research_bundle(market_id)
        vault.write_research_bundle(market_id, shared_bundle, meta=shared_meta)
    elif refresh or not _bundle_has_entries(vault.read_research_bundle(market_id)):
        if refresh:
            vault.clear_research_bundle(market_id)
        queries = _plan_research_queries(vault, runner, row, today, shadow)
        if queries is None:
            return False
        _fetch_and_persist_bundle(vault, market_id, queries, runner, today)
    return True


def _collect_samples(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    directives: str,
    today: str,
    shadow: bool = False,
) -> list[Sample] | None:
    """Lead sample plus independent extra samples; ``None`` after an inactive flag."""
    # ``_research_loop`` is looked up here at call time so patches on this module still apply.
    return _collect_samples_impl(
        vault, runner, row, directives, today, shadow, research_loop=_research_loop
    )


def _write_research_report(
    vault: ObsidianManager,
    market_id: str,
    md: dict[str, Any],
    q: float | None,
    agg: dict[str, Any],
    samples: list[Sample],
    ev_source: str,
    ev_fallback: bool,
    indep: dict[str, Any] | None = None,
) -> bool:
    """Persist the chosen sample's report to ``02_Active_Research/``."""
    chosen, chosen_research = samples[agg["chosen"]]
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
        **(indep or {}),
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
    shared_bundle: list[dict[str, Any]] | None = None,
    shared_meta: dict[str, Any] | None = None,
    shadow: bool = False,
    quick: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """FULL stage for one market: Briefer -> A-IQ -> Deep Researcher x3 -> Hub decision.

    ``shadow`` (measure only, a quick no_divergence skip): runs the same research but writes no
    report and leaves ``research_state`` untouched; one ``shadow_research`` ledger row, returns None.

    Returns the Phase 4 row (``p_value``/``p_low``/``p_high``/``market_data``) when active
    research was written, ``None`` after a ledgered skip or an inactive flag.
    """
    if not shadow:
        return _research_market_body(
            vault, runner, row, directives, refresh, shared_bundle, shared_meta, False, quick
        )
    market_id = row["market_id"]
    try:
        return _research_market_body(
            vault, runner, row, directives, refresh, shared_bundle, shared_meta, True, quick
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
            **_ledger_market_fields(row.get("market_data"), role="deep_researcher"),
        )
        log.warning("[PHASE 3] shadow_failed for %s: %s", market_id, reason)
        return None


def _research_market_body(
    vault: ObsidianManager,
    runner: AgentRunner,
    row: dict[str, Any],
    directives: str,
    refresh: bool,
    shared_bundle: list[dict[str, Any]] | None,
    shared_meta: dict[str, Any] | None,
    shadow: bool,
    quick: dict[str, Any] | None,
) -> dict[str, Any] | None:
    market_id = row["market_id"]
    md = row.get("market_data") or {}
    q = scraper.select_q(md)
    today = today_date()

    if not _prepare_evidence(
        vault, runner, row, today, refresh, shared_bundle, shared_meta, shadow
    ):
        return None
    samples = _collect_samples(vault, runner, row, directives, today, shadow)
    if samples is None:
        return None

    agg = aggregate_samples(samples)
    ev_meta = vault.read_research_bundle_meta(market_id)
    ev_source = ev_meta.get("evidence_source") or evidence.AIQ  # legacy bundles were A-IQ
    ev_fallback = bool(ev_meta.get("evidence_fallback"))
    values = {
        "q": q, "p": agg["p"], "p_low": agg["p_low"], "p_high": agg["p_high"],
        "samples": agg["samples"], "spread": agg["spread"], "confidence": agg["confidence"],
        "evidence_source": ev_source, "evidence_fallback": ev_fallback,
    }
    indep: dict[str, Any] = {}
    if not shadow and evidence.is_indep_sample(market_id):
        indep = _indep_sample(vault, runner, row, directives, today, ev_source, agg["p"])
        values.update(indep)
    knobs = extract_filter_directives(directives)
    if shadow:
        would_skip = None
        if agg["spread"] > knobs["research_max_sample_spread"]:
            would_skip = "sample_disagreement"
        elif q is not None and abs(agg["p"] - q) > knobs["research_max_q_gap"]:
            would_skip = "probable_misread"
        quick = quick or {}
        _ledger_safe(
            vault, market_id, "shadow_research",
            shadow=True, would_skip=would_skip,
            quick_p=quick.get("p"), quick_p_low=quick.get("p_low"), quick_p_high=quick.get("p_high"),
            **values, **_ledger_market_fields(md, role="deep_researcher"),
        )
        log.info("[PHASE 3] shadow_research for %s (would_skip=%s)", market_id, would_skip)
        return None
    if agg["spread"] > knobs["research_max_sample_spread"]:
        _skip_research(vault, row, "full", "sample_disagreement", **values)
        return None
    if q is not None and abs(agg["p"] - q) > knobs["research_max_q_gap"]:
        _skip_research(vault, row, "full", "probable_misread", **values)
        return None


    if not _write_research_report(
        vault, market_id, md, q, agg, samples, ev_source, ev_fallback, indep
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
        **indep,
    }
