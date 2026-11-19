"""LLM cost statistics over ``llm_usage.jsonl`` rows (see :mod:`orchestrator.llm.usage`)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from orchestrator.stats.calc import _num
from orchestrator.stats.groups import _brier_by_field, _latest_scored


def _cost(r: dict[str, Any]) -> float:
    return _num(r.get("cost_usd")) or 0.0


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
    by_model: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for r in rows:
        m = by_model[str(r.get("model") or "unknown")]
        m[0] += int(_num(r.get("kept")) or 0)
        m[1] += int(_num(r.get("dropped_no_url")) or 0)
        m[2] += int(_num(r.get("dropped_unretrieved")) or 0)

    def pack(v: list[int]) -> dict[str, Any]:
        total = sum(v)
        return {"kept": v[0], "dropped_no_url": v[1], "dropped_unretrieved": v[2],
                "drop_rate": (v[1] + v[2]) / total if total else None}

    tot = [sum(v[i] for v in by_model.values()) for i in range(3)]
    return {"n_calls": len(rows), "total": pack(tot), "by_model": {m: pack(v) for m, v in sorted(by_model.items())}}


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
        "n_unknown": len(unknown),
        "missing_models": missing_models,
    }
