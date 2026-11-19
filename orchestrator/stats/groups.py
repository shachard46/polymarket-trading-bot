"""Ledger-row groupings: resolutions, latest-decision-per-market, Brier by group."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from orchestrator import ledger
from orchestrator.stats.calc import _brier_group, _brier_or_none, _num, brier

MIN_FIT_N = 50
_SKIP_STAGES = ("gate_reject", "triage_reject", "research_skip")
OFFER_FINAL_STAGES = ("offer_declined", "offer_expired", "offer_stale")
_COUNTERFACTUAL_STAGES = ("below_edge", "research_skip") + OFFER_FINAL_STAGES
SHADOW_STAGE = "shadow_research"  # measure-only rows; never part of the main Brier / P&L
TRIAGE_SHADOW_STAGE = "triage_shadow"
COVERAGE_STAGE = "signal_coverage"
# Measure-only / bookkeeping stages: never part of the main Brier, P&L or lambda paths.
_NON_DECISION = (
    ledger.RESOLUTION_STAGE, ledger.POST_MORTEM_STAGE, SHADOW_STAGE,
    TRIAGE_SHADOW_STAGE, "shadow_failed", COVERAGE_STAGE,
    "offer_pending", "offer_approved", "user_vote",
)
_COVERAGE_SIGNALS = (
    "volume_24h", "day_price_change", "bid_ask", "holders", "price_history", "trades", "book",
)
_INTEG_REASONS = ("ramp_thin", "wallet_dominated", "rules_changed", "disputed_before")
_GATE_FLAG_REASONS = ("suspicious_move", "concentrated", "wide_spread")


def resolutions(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    return {
        r["market_id"]: int(r["outcome_yes"])
        for r in rows
        if r.get("stage") == ledger.RESOLUTION_STAGE and r.get("outcome_yes") in (0, 1)
    }


def _latest_by_market(rows: Iterable[dict[str, Any]], stages: Iterable[str] | None = None):
    wanted = set(stages) if stages is not None else None
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        st = r.get("stage")
        if st in _NON_DECISION or not r.get("market_id") or r.get("market_id") == "_tick":
            continue
        if wanted is None or st in wanted:
            out[r["market_id"]] = r
    return out


def _latest_scored(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    res = resolutions(rows)
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("stage") in _NON_DECISION:
            continue
        if r.get("market_id") in res and _num(r.get("p")) is not None and _num(r.get("q")) is not None:
            out[r["market_id"]] = r
    return out


def scored_triples(rows: list[dict[str, Any]]) -> list[tuple[float, float, int]]:
    """``(p, q, outcome)`` from each resolved market's latest decision having both p and q."""
    res = resolutions(rows)
    return [(_num(r["p"]), _num(r["q"]), res[m]) for m, r in _latest_scored(rows).items()]


def brier_by_directive_version(rows: list[dict[str, Any]]) -> dict[int, tuple[int, float]]:
    """``{directive_version: (n_scored, model Brier)}`` over resolved markets.

    A market is attributed to the ``directive_version`` stamped on its latest decision row that
    carries both ``p`` and ``q`` (the version that produced the estimate).
    """
    res = resolutions(rows)
    groups: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for m, r in _latest_scored(rows).items():
        try:
            version = int(r.get("directive_version") or 0)
        except (TypeError, ValueError):
            continue
        groups[version].append((_num(r["p"]), res[m]))
    return {
        v: (len(pairs), brier([p for p, _ in pairs], [y for _, y in pairs]))
        for v, pairs in groups.items()
    }


def _brier_by_field(rows: list[dict[str, Any]], field: str) -> dict[str, tuple[int, float, float]]:
    """``{value of field: (n, brier_p, brier_p - brier_q)}`` over resolved scored markets (absent: ``unknown``)."""
    res = resolutions(rows)
    groups: dict[str, list[tuple[float, float, int]]] = defaultdict(list)
    for m, r in _latest_scored(rows).items():
        groups[str(r.get(field) or "unknown")].append((_num(r["p"]), _num(r["q"]), res[m]))
    out = {}
    for key, t in sorted(groups.items()):
        ys = [y for _, _, y in t]
        bp = brier([p for p, _, _ in t], ys)
        out[key] = (len(t), bp, bp - brier([q for _, q, _ in t], ys))
    return out


def brier_by_evidence_source(rows: list[dict[str, Any]]) -> dict[str, tuple[int, float, float]]:
    """``{evidence_source: (n, brier_p, brier_p - brier_q)}`` over resolved scored markets."""
    return _brier_by_field(rows, "evidence_source")


def brier_by_judge_arm(rows: list[dict[str, Any]]) -> dict[str, tuple[int, float, float]]:
    """``{judge_arm: (n, brier_p, brier_p - brier_q)}`` over resolved scored markets (``one`` vs ``two`` judges)."""
    return _brier_by_field(rows, "judge_arm")


def brier_by_runner_backend(rows: list[dict[str, Any]]) -> dict[str, tuple[int, float, float]]:
    """``{runner_backend: (n, brier_p, brier_p - brier_q)}`` over resolved scored markets."""
    return _brier_by_field(rows, "runner_backend")


def brier_by_model(rows: list[dict[str, Any]]) -> dict[str, tuple[int, float, float]]:
    """``{model: (n, brier_p, brier_p - brier_q)}`` over resolved scored markets (absent: ``unknown``)."""
    return _brier_by_field(rows, "model")


def brier_by_provider(rows: list[dict[str, Any]]) -> dict[str, tuple[int, float, float]]:
    """``{provider: (n, brier_p, brier_p - brier_q)}`` over resolved scored markets (absent: ``unknown``)."""
    return _brier_by_field(rows, "provider")


def brier_shadow(
    rows: list[dict[str, Any]],
) -> tuple[int, float | None, float | None, float | None]:
    """``(n, Brier(quick p), Brier(full p), Brier(q))`` over resolved shadow markets.

    Uses each market's latest ``shadow_research`` row having quick_p, p and q (Brier is ``None`` if n = 0).
    """
    res = resolutions(rows)
    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("stage") == SHADOW_STAGE and r.get("market_id") in res:
            if all(_num(r.get(k)) is not None for k in ("quick_p", "p", "q")):
                latest[r["market_id"]] = r
    if not latest:
        return 0, None, None, None
    ys = [res[m] for m in latest]
    cols = [[_num(r[k]) for r in latest.values()] for k in ("quick_p", "p", "q")]
    return (len(ys), *(brier(c, ys) for c in cols))


def triage_shadow_stats(rows: list[dict[str, Any]]) -> dict[str, tuple[int, float | None, float | None]]:
    """`{kind: (n, Brier(quick p), Brier(q))}` over resolved triage_shadow markets (no error row).

    kinds: `not_researchable`, `manipulation_risk`, `all`. Latest triage_shadow row per market.
    """
    res = resolutions(rows)
    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("stage") == TRIAGE_SHADOW_STAGE and r.get("market_id") in res and not r.get("error"):
            if _num(r.get("p")) is not None and _num(r.get("q")) is not None:
                latest[r["market_id"]] = r
    out = {}
    for kind in ("not_researchable", "manipulation_risk", "all"):
        sel = [(m, r) for m, r in latest.items() if kind == "all" or r.get("triage_kind") == kind
               or (r.get("triage_kind") is None and (r.get("reason") == "manipulation_risk") == (kind == "manipulation_risk"))]
        n, bp = _brier_group([(_num(r["p"]), res[m]) for m, r in sel])
        out[kind] = (n, bp, _brier_group([(_num(r["q"]), res[m]) for m, r in sel])[1])
    return out


def signal_coverage_stats(rows: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    """Latest-tick and 7-day `(missing, total)` per signal from `signal_coverage` rows."""
    now = now or datetime.now(tz=timezone.utc)
    cov = [r for r in rows if r.get("stage") == COVERAGE_STAGE]
    latest = cov[-1] if cov else None
    out: dict[str, Any] = {"ticks": len(cov), "signals": {}}
    for sig in _COVERAGE_SIGNALS:
        lm = lt = wm = wt = 0
        if latest and isinstance(latest.get(sig), dict):
            lm, lt = int(latest[sig].get("missing") or 0), int(latest[sig].get("total") or 0)
        for r in cov:
            try:
                age = (now - datetime.fromisoformat(str(r.get("ts")))).total_seconds()
            except (TypeError, ValueError):
                continue
            if age <= 7 * 86400 and isinstance(r.get(sig), dict):
                wm += int(r[sig].get("missing") or 0)
                wt += int(r[sig].get("total") or 0)
        out["signals"][sig] = {"latest": (lm, lt), "week": (wm, wt)}
    return out


def gate_flag_stats(rows: list[dict[str, Any]]) -> dict[str, tuple[int, float | None]]:
    """`{group: (n, Brier(q))}` on resolved markets: gate_reject by flag reason vs markets that passed the gate.

    Uses each market's latest `gate_reject` row (reasons suspicious_move/concentrated/wide_spread);
    'passed_gate' = resolved markets with any non-gate_reject decision row (reached triage or beyond).
    Holder-based flags are further split by `holder_basis` when present (e.g. `concentrated/supply`).
    """
    res = resolutions(rows)
    groups: dict[str, dict[str, float]] = defaultdict(dict)
    rejected: dict[str, dict[str, Any]] = {}
    passed: dict[str, float] = {}
    for r in rows:
        st, m = r.get("stage"), r.get("market_id")
        if st in _NON_DECISION or not m or m == "_tick" or m not in res or _num(r.get("q")) is None:
            continue
        if st == "gate_reject":
            if r.get("reason") in _GATE_FLAG_REASONS:
                rejected[m] = r
            else:
                rejected.pop(m, None)
        elif st in ("triage_reject", "research_skip", "below_edge", "traded"):
            passed[m] = _num(r["q"])
    for m, r in rejected.items():
        if m in passed:
            continue
        key = r["reason"] + (f"/{r['holder_basis']}" if r.get("holder_basis") and r["reason"] == "concentrated" else "")
        groups[key][m] = _num(r["q"])
        groups[r["reason"]][m] = _num(r["q"])
    groups["passed_gate"] = passed
    return {key: _brier_group([(q, res[m]) for m, q in d.items()]) for key, d in sorted(groups.items())}


def market_integrity_stats(rows: list[dict[str, Any]]) -> dict[str, dict[str, tuple[int, float | None]]]:
    """Brier(q) on resolved markets by integ_score band, and by the integrity gate-reject reasons.

    ``bands``: latest decision row's ``integ_score`` (>=0.8, 0.5-0.8, <0.5, unknown).
    ``rejects``: latest ``gate_reject`` per market with one of the integrity reasons.
    Each value is ``(n, Brier(q))``; q is the market's latest decision-row q.
    """
    res = resolutions(rows)
    q_by: dict[str, float] = {}
    score_by: dict[str, float | None] = {}
    rej_by: dict[str, str] = {}
    for r in rows:
        st, m = r.get("stage"), r.get("market_id")
        if st in _NON_DECISION or not m or m == "_tick" or m not in res:
            continue
        q = _num(r.get("q"))
        if q is not None:
            q_by[m] = q
        sc = _num(r.get("integ_score"))
        if sc is not None:
            score_by[m] = sc
        if st == "gate_reject":
            if r.get("reason") in _INTEG_REASONS:
                rej_by[m] = r["reason"]
            else:
                rej_by.pop(m, None)
    bands: dict[str, list[str]] = {">=0.8": [], "0.5-0.8": [], "<0.5": [], "unknown": []}
    for m in q_by:
        sc = score_by.get(m)
        key = "unknown" if sc is None else ">=0.8" if sc >= 0.8 else "0.5-0.8" if sc >= 0.5 else "<0.5"
        bands[key].append(m)
    rejects: dict[str, list[str]] = {k: [] for k in _INTEG_REASONS}
    for m, reason in rej_by.items():
        if m in q_by:
            rejects[reason].append(m)

    def score_group(groups: dict[str, list[str]]) -> dict[str, tuple[int, float | None]]:
        return {k: _brier_group([(q_by[m], res[m]) for m in ids]) for k, ids in groups.items()}

    return {"bands": score_group(bands), "rejects": score_group(rejects)}


def failure_class_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Count of the latest ``post_mortem`` row's ``failure_class`` per market."""
    latest: dict[str, str] = {}
    for r in rows:
        if r.get("stage") == ledger.POST_MORTEM_STAGE and r.get("market_id"):
            latest[r["market_id"]] = str(r.get("failure_class") or "unclassified")
    counts: dict[str, int] = defaultdict(int)
    for fc in latest.values():
        counts[fc] += 1
    return dict(sorted(counts.items()))


def fit_lambda(rows: list[dict[str, Any]]) -> float | None:
    """λ in [0,1] minimising Brier of q + λ(p - q) over resolved rows; ``None`` if n < 50."""
    triples = scored_triples(rows)
    if len(triples) < MIN_FIT_N:
        return None
    best, best_loss = 0.0, float("inf")
    for i in range(101):
        lam = i / 100
        loss = sum((q + lam * (p - q) - y) ** 2 for p, q, y in triples)
        if loss < best_loss - 1e-12:
            best, best_loss = lam, loss
    return best


def false_negative_rate(rows: list[dict[str, Any]]) -> tuple[int, float | None]:
    """Proxy: share of rejected/skipped resolved markets where |outcome - q| > 0.5."""
    res = resolutions(rows)
    hits = n = 0
    for m, r in _latest_by_market(rows, _SKIP_STAGES).items():
        q = _num(r.get("q"))
        if m in res and q is not None:
            n += 1
            hits += abs(res[m] - q) > 0.5
    return n, (hits / n if n else None)
