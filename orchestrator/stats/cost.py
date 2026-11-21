"""LLM cost statistics over ``llm_usage.jsonl`` rows (see :mod:`orchestrator.llm.usage`)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from orchestrator.stats.calc import _num
from orchestrator.stats.groups import _brier_by_field, _latest_scored


def _cost(r: dict[str, Any]) -> float:
    return _num(r.get("cost_usd")) or 0.0


def _arm_cost(r: dict[str, Any]) -> float:
    """Cost for arm comparisons: the notional API cost of subscription (claude_code) calls, else the billed cost."""
    n = _num(r.get("notional_cost_usd"))
    return n if n is not None else _cost(r)


def _day(r: dict[str, Any]) -> str:
    return str(r.get("ts") or "")[:10]


def _sum_by(usage: list[dict[str, Any]], field: str) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for r in usage:
        out[str(r.get(field) or "unknown")] += _cost(r)
    return dict(sorted(out.items()))


def _thinking_by_role(usage: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for r in usage:
        out[str(r.get("role") or "unknown")] += int(_num(r.get("thinking_tokens")) or 0)
    return dict(sorted(out.items()))


def web_filter_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals and drop rate of the web-evidence filter, overall and by model."""
    by_model: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for r in rows:
        m = by_model[str(r.get("model") or "unknown")]
        m[0] += int(_num(r.get("kept")) or 0)
        m[1] += int(_num(r.get("dropped_no_url")) or 0)
        m[2] += int(_num(r.get("dropped_unretrieved")) or 0)
        m[3] += int(_num(r.get("dropped_unverifiable")) or 0)

    def pack(v: list[int]) -> dict[str, Any]:
        total = sum(v)
        return {"kept": v[0], "dropped_no_url": v[1], "dropped_unretrieved": v[2],
                "dropped_unverifiable": v[3],
                "drop_rate": (v[1] + v[2]) / total if total else None}

    tot = [sum(v[i] for v in by_model.values()) for i in range(4)]
    return {"n_calls": len(rows), "total": pack(tot), "by_model": {m: pack(v) for m, v in sorted(by_model.items())}}


def judge_arm_cost(usage: list[dict[str, Any]]) -> dict[str, tuple[int, float, float | None]]:
    """``{judge_arm: (n researched markets, judge cost USD, cost per researched market)}``.

    Usage rows are tagged with the arm at call time (``judge_arm``, set around the judge calls only), so the cost of
    quick-stage, helper and earlier-refresh calls never leaks into an arm; a row's market is its ``market_id``.
    """
    cost: dict[str, float] = defaultdict(float)
    markets: dict[str, set[str]] = defaultdict(set)
    for r in usage:
        arm = r.get("judge_arm")
        if not arm:
            continue
        cost[str(arm)] += _cost(r)
        if r.get("market_id"):
            markets[str(arm)].add(str(r["market_id"]))
    return {
        arm: (len(markets[arm]), cost[arm], cost[arm] / len(markets[arm]) if markets[arm] else None)
        for arm in sorted(cost)
    }


def judge_model_arm_cost(rows: list[dict[str, Any]], usage: list[dict[str, Any]]) -> dict[str, tuple[int, float, float | None]]:
    """``{judge_model_arm: (n FULL-judged markets, judge cost USD, cost per market)}``.

    Judge usage rows (tagged ``judge_arm``, i.e. FULL-stage judge calls only) are joined to the market's
    ``judge_model_arm`` from the ledger rows; markets without that field are skipped.
    """
    arm_of: dict[str, str] = {}
    for r in rows:
        if r.get("judge_model_arm") and r.get("market_id"):
            arm_of[str(r["market_id"])] = str(r["judge_model_arm"])
    cost: dict[str, float] = defaultdict(float)
    markets: dict[str, set[str]] = defaultdict(set)
    for u in usage:
        arm = arm_of.get(str(u.get("market_id")))
        if not u.get("judge_arm") or not arm:
            continue
        cost[arm] += _arm_cost(u)
        markets[arm].add(str(u["market_id"]))
    return {a: (len(markets[a]), cost[a], cost[a] / len(markets[a])) for a in sorted(cost)}


def judge_model_arm_web_cost(rows: list[dict[str, Any]], usage: list[dict[str, Any]]) -> dict[str, tuple[int, float, float | None]]:
    """``{judge_model_arm: (n markets with web usage, web_searcher + web_extractor cost USD, cost per market)}``."""
    arm_of = {str(r["market_id"]): str(r["judge_model_arm"]) for r in rows if r.get("judge_model_arm") and r.get("market_id")}
    cost: dict[str, float] = defaultdict(float)
    markets: dict[str, set[str]] = defaultdict(set)
    for u in usage:
        arm = arm_of.get(str(u.get("market_id")))
        if u.get("role") not in ("web_searcher", "web_extractor") or not arm:
            continue
        cost[arm] += _arm_cost(u)
        markets[arm].add(str(u["market_id"]))
    return {a: (len(markets[a]), cost[a], cost[a] / len(markets[a])) for a in sorted(cost)}


def cost_stats(rows: list[dict[str, Any]], usage: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    """Cost per day / role / model / market, Brier-per-dollar by model, unknown-price counts."""
    now = now or datetime.now(tz=timezone.utc)
    days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    per_day = {d: 0.0 for d in days}
    for r in usage:
        if _day(r) in per_day:
            per_day[_day(r)] += _cost(r)
    unknown = [r for r in usage if r.get("cost_usd") is None]
    missing_models = sorted({str(r.get("model") or "unknown") for r in unknown})
    by_market = _sum_by([r for r in usage if r.get("market_id")], "market_id")
    # Brier per dollar: cost of a model's calls on its scored markets, per scored market.
    scored = _latest_scored(rows)
    by_model_brier = _brier_by_field(rows, "model")
    spend: dict[str, float] = defaultdict(float)
    for r in usage:
        if r.get("market_id") in scored:
            spend[str(r.get("model") or "unknown")] += _cost(r)
    per_dollar = {}
    for model, (n, bp, diff) in by_model_brier.items():
        per_dollar[model] = (n, diff, spend.get(model, 0.0) / n if n else None)
    return {
        "n_calls": len(usage),
        "today": per_day[days[0]],
        "per_day": per_day,
        "total": sum(_cost(r) for r in usage),
        "by_role": _sum_by(usage, "role"),
        "thinking_by_role": _thinking_by_role(usage),
        "by_model": _sum_by(usage, "model"),
        "by_market": by_market,
        "brier_per_dollar": per_dollar,
        "judge_arm_cost": judge_arm_cost(usage),
        "judge_model_arm_cost": judge_model_arm_cost(rows, usage),
        "judge_model_arm_web_cost": judge_model_arm_web_cost(rows, usage),
        "n_unknown": len(unknown),
        "missing_models": missing_models,
    }
