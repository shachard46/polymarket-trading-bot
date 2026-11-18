"""P&L over ledger rows: per-bet P&L, annual-offset tax, offers & votes."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable

from config import trading_constants
from orchestrator import ledger
from orchestrator.stats.calc import _brier_or_none, _num
from orchestrator.stats.groups import _latest_by_market, resolutions

TAX = getattr(trading_constants, "TAX", 0.25)


def _pnl(row: dict[str, Any], outcome_yes: int, default_size: float | None = None) -> float | None:
    """Pre-tax P&L of a stated bet (tax is levied on the annual net, see :func:`tax_by_year`)."""
    price = _num(row.get("price"))
    size = _num(row.get("size_usd"))
    if size is None:
        size = default_size
    if price is None or not 0 < price < 1 or size is None or size <= 0:
        return None
    shares = _num(row.get("shares")) or size / price
    side = str(row.get("side") or "YES").upper()
    won = outcome_yes == (1 if side == "YES" else 0)
    return shares * (1 if won else 0) - size


def bet_pnl(row: dict[str, Any], outcome_yes: int) -> float | None:
    """Per-bet realised pre-tax P&L (``price``, ``size_usd``/``shares``, ``side``)."""
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

    Tax = TAX * max(0, net per resolution year).
    """
    res = resolutions(rows)
    years = resolution_years(rows)
    pairs = _pnl_groups(rows, res, stages, default_size)
    total = sum(p for _, p in pairs)
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


def realised_pnl(rows: list[dict[str, Any]]) -> float:
    """Total realised AFTER-tax P&L of resolved traded markets (annual-net tax)."""
    return pnl_summary(rows, ("traded",))["after_tax"]


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
