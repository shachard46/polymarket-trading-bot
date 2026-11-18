"""Measurement: calibration, Brier / log-loss, P&L and λ fit over the decision ledger.

Pure functions over ledger rows (dicts); :func:`write_stats` renders
``<vault>/00_System/stats.md``.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from config import trading_constants
from obsidian_utils import ObsidianManager
from orchestrator import ledger

TAX = getattr(trading_constants, "TAX", 0.25)
TAX_MODE = getattr(trading_constants, "TAX_MODE", "annual_offset")
_EPS = 1e-6
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


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


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


def scored_triples(rows: list[dict[str, Any]]) -> list[tuple[float, float, int]]:
    """``(p, q, outcome)`` from each resolved market's latest decision having both p and q."""
    res = resolutions(rows)
    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("stage") in _NON_DECISION:
            continue
        if r.get("market_id") not in res:
            continue
        if _num(r.get("p")) is not None and _num(r.get("q")) is not None:
            latest[r["market_id"]] = r
    return [(_num(r["p"]), _num(r["q"]), res[m]) for m, r in latest.items()]


def _latest_scored(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    res = resolutions(rows)
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("stage") in _NON_DECISION:
            continue
        if r.get("market_id") in res and _num(r.get("p")) is not None and _num(r.get("q")) is not None:
            out[r["market_id"]] = r
    return out


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


def brier_by_evidence_source(rows: list[dict[str, Any]]) -> dict[str, tuple[int, float, float]]:
    """``{evidence_source: (n, brier_p, brier_p - brier_q)}`` over resolved scored markets.

    Attributed via the latest scored decision row's ``evidence_source`` (``unknown`` if absent).
    """
    res = resolutions(rows)
    groups: dict[str, list[tuple[float, float, int]]] = defaultdict(list)
    for m, r in _latest_scored(rows).items():
        groups[str(r.get("evidence_source") or "unknown")].append((_num(r["p"]), _num(r["q"]), res[m]))
    out = {}
    for src, t in sorted(groups.items()):
        ys = [y for _, _, y in t]
        bp = brier([p for p, _, _ in t], ys)
        out[src] = (len(t), bp, bp - brier([q for _, q, _ in t], ys))
    return out


def brier_by_runner_backend(rows: list[dict[str, Any]]) -> dict[str, tuple[int, float, float]]:
    """``{runner_backend: (n, brier_p, brier_p - brier_q)}`` over resolved scored markets (absent field: ``unknown``)."""
    res = resolutions(rows)
    groups: dict[str, list[tuple[float, float, int]]] = defaultdict(list)
    for m, r in _latest_scored(rows).items():
        groups[str(r.get("runner_backend") or "unknown")].append((_num(r["p"]), _num(r["q"]), res[m]))
    out = {}
    for key, t in sorted(groups.items()):
        ys = [y for _, _, y in t]
        bp = brier([p for p, _, _ in t], ys)
        out[key] = (len(t), bp, bp - brier([q for _, q, _ in t], ys))
    return out


_OFFER_GROUPS = {
    "offer_approved": "approved",
    "offer_declined": "declined",
    "offer_expired": "expired",
    "offer_stale": "stale",
}


def offers_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Offers & votes: counts per outcome plus Brier / counterfactual P&L per resolved group.

    ``pending`` = offers with an ``offer_pending`` row and no later outcome row for the same offer_id
    (rows without offer_id fall back to market_id). Per-group Brier and P&L use that group's offer rows
    (the stated side/price/size_usd of the offer), once the market resolved.
    """
    res = resolutions(rows)
    pending: dict[str, None] = {}
    counts = {g: 0 for g in _OFFER_GROUPS.values()}
    group_rows: dict[str, list[dict[str, Any]]] = {g: [] for g in _OFFER_GROUPS.values()}
    for r in rows:
        stage = r.get("stage")
        key = str(r.get("offer_id") or r.get("market_id"))
        if stage == "offer_pending":
            pending[key] = None
        elif stage in _OFFER_GROUPS:
            pending.pop(key, None)
            counts[_OFFER_GROUPS[stage]] += 1
            group_rows[_OFFER_GROUPS[stage]].append(r)
    groups: dict[str, dict[str, Any]] = {}
    for g, grs in group_rows.items():
        scored = [r for r in grs if r.get("market_id") in res and _num(r.get("p")) is not None and _num(r.get("q")) is not None]
        ys = [res[r["market_id"]] for r in scored]
        pnls = [x for x in (_pnl(r, res[r["market_id"]], 1.0) for r in grs if r.get("market_id") in res) if x is not None]
        groups[g] = {
            "n_resolved": len([r for r in grs if r.get("market_id") in res]),
            "brier_p": _brier_or_none([_num(r["p"]) for r in scored], ys),
            "brier_q": _brier_or_none([_num(r["q"]) for r in scored], ys),
            "pnl": sum(pnls),
        }
    return {"pending": len(pending), "counts": counts, "groups": groups}


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


def _brier_or_none(preds: list[float], ys: list[int]) -> float | None:
    return brier(preds, ys) if ys else None


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
        ys = [res[m] for m, _ in sel]
        out[kind] = (
            len(ys),
            _brier_or_none([_num(r["p"]) for _, r in sel], ys),
            _brier_or_none([_num(r["q"]) for _, r in sel], ys),
        )
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


def independent_sample_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Research rows carrying `indep_p`: n, mean |indep_gap|, and for resolved ones Brier(median p) vs Brier(indep_p)."""
    res = resolutions(rows)
    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("stage") in _NON_DECISION or not r.get("market_id") or r.get("market_id") == "_tick":
            continue
        if _num(r.get("indep_p")) is not None:
            latest[r["market_id"]] = r
    gaps = []
    for r in latest.values():
        gap = _num(r.get("indep_gap"))
        if gap is None and _num(r.get("p")) is not None:
            gap = _num(r["p"]) - _num(r["indep_p"])
        if gap is not None:
            gaps.append(abs(gap))
    scored = [(m, r) for m, r in latest.items() if m in res and _num(r.get("p")) is not None]
    ys = [res[m] for m, _ in scored]
    return {
        "n": len(latest),
        "mean_abs_gap": sum(gaps) / len(gaps) if gaps else None,
        "n_resolved": len(ys),
        "brier_p": _brier_or_none([_num(r["p"]) for _, r in scored], ys),
        "brier_indep": _brier_or_none([_num(r["indep_p"]) for _, r in scored], ys),
    }


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
    out = {}
    for key, d in sorted(groups.items()):
        ids = list(d)
        out[key] = (len(ids), _brier_or_none([d[m] for m in ids], [res[m] for m in ids]))
    return out


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
        return {
            k: (len(ids), _brier_or_none([q_by[m] for m in ids], [res[m] for m in ids]))
            for k, ids in groups.items()
        }

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


def brier(preds: list[float], outcomes: list[int]) -> float:
    return sum((p - y) ** 2 for p, y in zip(preds, outcomes)) / len(preds)


def log_loss(preds: list[float], outcomes: list[int]) -> float:
    total = 0.0
    for p, y in zip(preds, outcomes):
        p = min(max(p, _EPS), 1 - _EPS)
        total -= math.log(p) if y else math.log(1 - p)
    return total / len(preds)


def bootstrap_diff_ci(
    triples: list[tuple[float, float, int]], n_boot: int = 1000, seed: int = 0
) -> tuple[float, float]:
    """90% bootstrap CI of Brier(p) - Brier(q); negative means the model beats the market."""
    rng = random.Random(seed)
    n = len(triples)
    sq = [((p - y) ** 2 - (q - y) ** 2) for p, q, y in triples]
    diffs = sorted(sum(sq[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    return diffs[int(0.05 * n_boot)], diffs[min(int(0.95 * n_boot), n_boot - 1)]


def calibration_table(triples: list[tuple[float, float, int]]) -> list[dict[str, Any]]:
    buckets: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for p, _q, y in triples:
        buckets[min(int(p * 10), 9)].append((p, y))
    return [
        {
            "bucket": f"{b * 10}-{b * 10 + 10}%",
            "n": len(v),
            "mean_p": sum(p for p, _ in v) / len(v),
            "hit_rate": sum(y for _, y in v) / len(v),
        }
        for b, v in sorted(buckets.items())
    ]


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


def _pnl(row: dict[str, Any], outcome_yes: int, default_size: float | None = None) -> float | None:
    """P&L of a stated bet: pre-tax in ``annual_offset`` mode, tax on a winning trade in ``per_trade``."""
    price = _num(row.get("price"))
    size = _num(row.get("size_usd"))
    if size is None:
        size = default_size
    if price is None or not 0 < price < 1 or size is None or size <= 0:
        return None
    shares = _num(row.get("shares")) or size / price
    side = str(row.get("side") or "YES").upper()
    won = outcome_yes == (1 if side == "YES" else 0)
    profit = shares * (1 if won else 0) - size
    if TAX_MODE == "per_trade" and profit > 0:
        return profit * (1 - TAX)
    return profit


def bet_pnl(row: dict[str, Any], outcome_yes: int) -> float | None:
    """Per-bet realised P&L (``price``, ``size_usd``/``shares``, ``side``): pre-tax in
    ``annual_offset`` mode (tax is levied on the annual net, see :func:`tax_by_year`)."""
    return _pnl(row, outcome_yes)


def _pnl_groups(rows, res, stages, default_size=None) -> list[tuple[dict[str, Any], float]]:
    out = []
    for m, r in _latest_by_market(rows, stages).items():
        if m in res:
            pnl = _pnl(r, res[m], default_size)
            if pnl is not None:
                out.append((r, pnl))
    return out


def resolution_years(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    """``{market_id: calendar year}`` of each market's (latest) resolution row ``ts`` (UTC)."""
    out: dict[str, int] = {}
    for r in rows:
        if r.get("stage") == ledger.RESOLUTION_STAGE and r.get("market_id"):
            try:
                out[r["market_id"]] = datetime.fromisoformat(str(r.get("ts"))).year
            except ValueError:
                continue
    return out


def tax_by_year(pnl_by_year: dict[int, float]) -> dict[int, float]:
    """``TAX * max(0, net pre-tax P&L)`` per calendar year (losses offset gains within the year)."""
    return {y: TAX * max(0.0, v) for y, v in sorted(pnl_by_year.items())}


def pnl_summary(rows: list[dict[str, Any]], stages, default_size=None) -> dict[str, Any]:
    """Pre-tax / tax / after-tax totals over the latest bet per resolved market in ``stages``.

    ``annual_offset``: tax = TAX * max(0, net per resolution year). ``per_trade``: per-bet P&L is
    already taxed, so no gross-up is attempted: ``pre_tax`` and ``after_tax`` both equal that
    total and ``tax`` is 0.
    """
    res = resolutions(rows)
    years = resolution_years(rows)
    pairs = _pnl_groups(rows, res, stages, default_size)
    total = sum(p for _, p in pairs)
    if TAX_MODE == "per_trade":
        return {"pre_tax": total, "tax": 0.0, "after_tax": total, "tax_by_year": {}, "n": len(pairs)}
    by_year: dict[int, float] = defaultdict(float)
    for r, pnl in pairs:
        by_year[years.get(r["market_id"], 0)] += pnl
    taxes = tax_by_year(by_year)
    tax = sum(taxes.values())
    return {"pre_tax": total, "tax": tax, "after_tax": total - tax, "tax_by_year": taxes, "n": len(pairs)}


def _group(pairs, key) -> dict[str, tuple[int, float]]:
    agg: dict[str, list[float]] = defaultdict(list)
    for r, pnl in pairs:
        agg[str(r.get(key) or "unknown")].append(pnl)
    return {k: (len(v), sum(v)) for k, v in sorted(agg.items())}


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


def realised_pnl(rows: list[dict[str, Any]]) -> float:
    """Total realised AFTER-tax P&L of resolved traded markets (annual-net tax in ``annual_offset``)."""
    return pnl_summary(rows, ("traded",))["after_tax"]


def compute_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    res = resolutions(rows)
    triples = scored_triples(rows)
    out: dict[str, Any] = {"n_resolved": len(res), "n_scored": len(triples)}
    if triples:
        ps = [t[0] for t in triples]
        qs = [t[1] for t in triples]
        ys = [t[2] for t in triples]
        out.update(
            brier_p=brier(ps, ys),
            brier_q=brier(qs, ys),
            logloss_p=log_loss(ps, ys),
            logloss_q=log_loss(qs, ys),
            brier_diff=brier(ps, ys) - brier(qs, ys),
            brier_diff_ci=bootstrap_diff_ci(triples),
        )
    out["brier_shadow"] = brier_shadow(rows)
    out["triage_shadow"] = triage_shadow_stats(rows)
    out["signal_coverage"] = signal_coverage_stats(rows)
    out["indep_sample"] = independent_sample_stats(rows)
    out["gate_flags"] = gate_flag_stats(rows)
    out["market_integrity"] = market_integrity_stats(rows)
    out["calibration"] = calibration_table(triples)
    out["brier_by_evidence_source"] = brier_by_evidence_source(rows)
    out["brier_by_runner_backend"] = brier_by_runner_backend(rows)
    out["offers"] = offers_stats(rows)
    traded = _pnl_groups(rows, res, ("traded",))
    summ = pnl_summary(rows, ("traded",))
    out["pnl_total"] = summ["pre_tax"]
    out["pnl_tax"] = summ["tax"]
    out["pnl_after_tax"] = summ["after_tax"]
    out["pnl_tax_by_year"] = summ["tax_by_year"]
    out["pnl_n"] = len(traded)
    out["pnl_by_category"] = _group(traded, "category")
    out["pnl_by_reason"] = _group(traded, "reason")
    cf = _pnl_groups(rows, res, _COUNTERFACTUAL_STAGES, default_size=1.0)
    out["counterfactual_by_stage"] = _group(cf, "stage")
    out["counterfactual_after_tax"] = pnl_summary(rows, _COUNTERFACTUAL_STAGES, default_size=1.0)
    out["false_negative"] = false_negative_rate(rows)
    out["lambda"] = fit_lambda(rows)
    return out


def _fmt(x: Any, nd: int = 4) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def _tax_desc() -> str:
    if TAX_MODE == "per_trade":
        return f"per-trade mode: {int(TAX * 100)}% tax on each winning trade, no offset"
    return f"annual_offset: {int(TAX * 100)}% of net gains per calendar year, losses offset within the year"


def render_stats(s: dict[str, Any]) -> str:
    L = [
        "# Stats",
        "",
        f"_Generated {datetime.now(tz=timezone.utc).isoformat()}_",
        "",
        f"Resolved markets: **{s['n_resolved']}** (scored with p and q: {s['n_scored']})",
        "",
        "## Model vs market",
        "",
    ]
    if "brier_p" in s:
        lo, hi = s["brier_diff_ci"]
        L += [
            "| metric | model (p) | market (q) |",
            "|---|---|---|",
            f"| Brier | {_fmt(s['brier_p'])} | {_fmt(s['brier_q'])} |",
            f"| Log-loss | {_fmt(s['logloss_p'])} | {_fmt(s['logloss_q'])} |",
            "",
            f"Brier(p) - Brier(q) = {_fmt(s['brier_diff'])}, bootstrap 90% CI [{_fmt(lo)}, {_fmt(hi)}] "
            "(negative = model better).",
        ]
    else:
        L.append("No resolved markets with both p and q yet.")
    L += ["", "### Brier by evidence source", "", "| evidence_source | n | Brier(p) | Brier(p) - Brier(q) |", "|---|---|---|---|"]
    L += [
        f"| {k} | {n} | {_fmt(bp)} | {_fmt(d)} |"
        for k, (n, bp, d) in s.get("brier_by_evidence_source", {}).items()
    ]
    L += ["", "### Brier by runner backend", "", "| runner_backend | n | Brier(p) | Brier(p) - Brier(q) |", "|---|---|---|---|"]
    L += [
        f"| {k} | {n} | {_fmt(bp)} | {_fmt(d)} |"
        for k, (n, bp, d) in s.get("brier_by_runner_backend", {}).items()
    ]
    n_sh, b_quick, b_full, b_mkt = s.get("brier_shadow", (0, None, None, None))
    L += [
        "", "### Shadow: quick vs full vs market", "",
        f"Resolved shadow markets (quick no_divergence skips run through FULL anyway): {n_sh}.",
        "", "| quick p | full p | market q |", "|---|---|---|",
        f"| {_fmt(b_quick)} | {_fmt(b_full)} | {_fmt(b_mkt)} |",
    ]
    L += ["", "### Signal coverage (missing / total of gated markets)", "",
          "| signal | latest tick | % | last 7 days | % |", "|---|---|---|---|---|"]
    alert = trading_constants.SIGNAL_COVERAGE_ALERT_FRAC
    for sig, d in s.get("signal_coverage", {}).get("signals", {}).items():
        cells = []
        for m, t in (d["latest"], d["week"]):
            frac = m / t if t else None
            flag = " ⚠" if frac is not None and frac > alert else ""
            cells += [f"{m}/{t}", f"{_fmt(None if frac is None else 100 * frac, 1)}%{flag}"]
        L.append(f"| {sig} | " + " | ".join(cells) + " |")
    L += ["", "### Triage shadow: quick p vs market (resolved triage rejects)", "",
          "| reason | n | Brier(quick p) | Brier(q) |", "|---|---|---|---|"]
    for kind, (n, bp, bq) in s.get("triage_shadow", {}).items():
        L.append(f"| {kind} | {n} | {_fmt(bp)} | {_fmt(bq)} |")
    ind = s.get("indep_sample", {"n": 0, "mean_abs_gap": None, "n_resolved": 0, "brier_p": None, "brier_indep": None})
    L += ["", "### Independent evidence sample", "",
          f"Research rows with indep_p: {ind['n']}; mean |indep_gap|: {_fmt(ind['mean_abs_gap'])}. "
          f"Resolved: {ind['n_resolved']}; Brier(median p) {_fmt(ind['brier_p'])} vs Brier(indep_p) {_fmt(ind['brier_indep'])}."]
    L += ["", "### Gate flags vs market calibration", "",
          "Brier(q) on resolved markets, by gate flag vs markets that passed the gate.", "",
          "| group | n | Brier(q) |", "|---|---|---|"]
    for key, (n, bq) in s.get("gate_flags", {}).items():
        L.append(f"| {key} | {n} | {_fmt(bq)} |")
    mi = s.get("market_integrity") or {"bands": {}, "rejects": {}}
    L += ["", "### Market integrity", "",
          "Brier(q) on resolved markets by integrity score band, and by integrity gate-reject reason.", "",
          "| group | n | Brier(q) |", "|---|---|---|"]
    for key, (n, bq) in mi["bands"].items():
        L.append(f"| score {key} | {n} | {_fmt(bq)} |")
    for key, (n, bq) in mi["rejects"].items():
        L.append(f"| gate_reject {key} | {n} | {_fmt(bq)} |")
    off = s.get("offers") or {"pending": 0, "counts": {}, "groups": {}}
    L += ["", "## Offers & votes", "", f"Pending: {off['pending']}. Approved {off['counts'].get('approved', 0)}, "
          f"declined {off['counts'].get('declined', 0)}, expired {off['counts'].get('expired', 0)}, "
          f"stale {off['counts'].get('stale', 0)}.", "",
          "| group | n resolved | Brier(p) | Brier(q) | hypothetical P&L USD |", "|---|---|---|---|---|"]
    for g, v in off["groups"].items():
        L.append(f"| {g} | {v['n_resolved']} | {_fmt(v['brier_p'])} | {_fmt(v['brier_q'])} | {v['pnl']:.2f} |")
    L += ["", "## Calibration (p in 10% buckets)", "", "| bucket | n | mean p | hit rate |", "|---|---|---|---|"]
    L += [f"| {c['bucket']} | {c['n']} | {_fmt(c['mean_p'], 3)} | {_fmt(c['hit_rate'], 3)} |" for c in s["calibration"]]
    L += [
        "",
        f"## Realised P&L (traded; {_tax_desc()})",
        "",
        f"Pre-tax: **{s['pnl_total']:.2f} USD** over {s['pnl_n']} resolved trades; "
        f"tax: {s['pnl_tax']:.2f} USD; after-tax: **{s['pnl_after_tax']:.2f} USD**.",
    ]
    L += [f"- {y}: tax {t:.2f} USD" for y, t in s["pnl_tax_by_year"].items()]
    for title, key in (("Per category", "pnl_by_category"), ("Per stage reason", "pnl_by_reason")):
        L += ["", f"### {title}", "", "| key | n | P&L USD |", "|---|---|---|"]
        L += [f"| {k} | {n} | {v:.2f} |" for k, (n, v) in s[key].items()]
    L += [
        "",
        "## Counterfactual P&L (hypothetical, at stated side/price; 1 USD stake if size missing)",
        "",
        "| stage | n | P&L USD |",
        "|---|---|---|",
    ]
    L += [f"| {k} | {n} | {v:.2f} |" for k, (n, v) in s["counterfactual_by_stage"].items()]
    cfs = s["counterfactual_after_tax"]
    L += ["", f"Counterfactual total: pre-tax {cfs['pre_tax']:.2f}, tax {cfs['tax']:.2f}, after-tax {cfs['after_tax']:.2f} USD."]
    n_fn, rate = s["false_negative"]
    L += [
        "",
        "## False-negative rate (PROXY)",
        "",
        f"Among {n_fn} resolved gate/triage/research rejects with q, share where |outcome - q| > 0.5: "
        f"{_fmt(rate, 3)}. This is a cheap proxy, not a true missed-edge rate.",
        "",
        "## Fitted lambda",
        "",
        f"argmin Brier of q + lambda(p - q): {_fmt(s['lambda'], 2)}"
        + (" (needs n >= 50)" if s["lambda"] is None else ""),
        "",
    ]
    return "\n".join(L)


def write_stats(vault: ObsidianManager) -> None:
    rows = list(ledger.iter_rows(vault))
    path = vault._dirs["system"] / "stats.md"
    path.write_text(render_stats(compute_stats(rows)), encoding="utf-8")


__all__ = ["brier_by_runner_backend", "offers_stats", "bet_pnl", "brier_by_evidence_source", "brier_by_directive_version", "brier_shadow", "triage_shadow_stats", "signal_coverage_stats", "independent_sample_stats", "gate_flag_stats", "market_integrity_stats", "failure_class_counts", "compute_stats", "fit_lambda", "pnl_summary", "realised_pnl", "render_stats", "tax_by_year", "write_stats"]
