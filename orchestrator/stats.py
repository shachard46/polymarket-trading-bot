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
_EPS = 1e-6
MIN_FIT_N = 50
_SKIP_STAGES = ("gate_reject", "triage_reject", "research_skip")
_COUNTERFACTUAL_STAGES = ("below_edge", "research_skip")


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
        if st == ledger.RESOLUTION_STAGE or not r.get("market_id"):
            continue
        if wanted is None or st in wanted:
            out[r["market_id"]] = r
    return out


def scored_triples(rows: list[dict[str, Any]]) -> list[tuple[float, float, int]]:
    """``(p, q, outcome)`` from each resolved market's latest decision having both p and q."""
    res = resolutions(rows)
    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("stage") == ledger.RESOLUTION_STAGE or r.get("market_id") not in res:
            continue
        if _num(r.get("p")) is not None and _num(r.get("q")) is not None:
            latest[r["market_id"]] = r
    return [(_num(r["p"]), _num(r["q"]), res[m]) for m, r in latest.items()]


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
    """P&L of a stated bet; tax applies to profit on wins only."""
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
    return profit * (1 - TAX) if profit > 0 else profit


def _pnl_groups(rows, res, stages, default_size=None) -> list[tuple[dict[str, Any], float]]:
    out = []
    for m, r in _latest_by_market(rows, stages).items():
        if m in res:
            pnl = _pnl(r, res[m], default_size)
            if pnl is not None:
                out.append((r, pnl))
    return out


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
    """Total realised (after-tax) P&L of resolved traded markets."""
    return sum(p for _, p in _pnl_groups(rows, resolutions(rows), ("traded",)))


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
    out["calibration"] = calibration_table(triples)
    traded = _pnl_groups(rows, res, ("traded",))
    out["pnl_total"] = sum(p for _, p in traded)
    out["pnl_n"] = len(traded)
    out["pnl_by_category"] = _group(traded, "category")
    out["pnl_by_reason"] = _group(traded, "reason")
    cf = _pnl_groups(rows, res, _COUNTERFACTUAL_STAGES, default_size=1.0)
    out["counterfactual_by_stage"] = _group(cf, "stage")
    out["false_negative"] = false_negative_rate(rows)
    out["lambda"] = fit_lambda(rows)
    return out


def _fmt(x: Any, nd: int = 4) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


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
    L += ["", "## Calibration (p in 10% buckets)", "", "| bucket | n | mean p | hit rate |", "|---|---|---|---|"]
    L += [f"| {c['bucket']} | {c['n']} | {_fmt(c['mean_p'], 3)} | {_fmt(c['hit_rate'], 3)} |" for c in s["calibration"]]
    L += [
        "",
        f"## Realised P&L (traded, {int(TAX * 100)}% tax on profit)",
        "",
        f"Total: **{s['pnl_total']:.2f} USD** over {s['pnl_n']} resolved trades.",
    ]
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


__all__ = ["compute_stats", "fit_lambda", "realised_pnl", "render_stats", "write_stats"]
