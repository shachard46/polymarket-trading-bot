"""Phase 2 helpers: integrity feature fill, filter-record integrity fields, signal-coverage counting."""

from __future__ import annotations

from typing import Any

from obsidian_utils import ObsidianManager
from config.trading_constants import SIGNAL_COVERAGE_ALERT_FRAC
from orchestrator import market_integrity, polymarket_api
from orchestrator.phases.common import _ledger_safe, log
from orchestrator.scraper import MarketRow

COVERAGE_SIGNALS = (
    "volume_24h", "day_price_change", "bid_ask", "holders", "price_history", "trades", "book",
)
Coverage = dict[str, list[int]]  # signal -> [missing, total]


def _new_coverage() -> Coverage:
    return {s: [0, 0] for s in COVERAGE_SIGNALS}


def _count_signal(coverage: Coverage | None, signal: str, missing: bool) -> None:
    if coverage is not None:
        coverage[signal][0] += int(missing)
        coverage[signal][1] += 1


def _count_gate_signals(coverage: Coverage | None, md: dict[str, Any]) -> None:
    _count_signal(coverage, "volume_24h", md.get("volume_24h") is None)
    _count_signal(coverage, "bid_ask", md.get("yes_bid") is None or md.get("yes_ask") is None)
    # day_price_change is counted in Phase 2 stage 2 (after the price-history fill), over in-budget markets.


def _integrity_state(md: dict[str, Any], prior: dict[str, Any] | None) -> dict[str, Any]:
    """Filter-record fields from the integrity step (empty when it did not run for this market)."""
    feats = {k: v for k, v in md.items() if k.startswith("integ_") and v is not None}
    if not feats:
        return {}
    out: dict[str, Any] = {}
    first = (prior or {}).get("description_first_hash") or md.get("integ_description_hash")
    if first:
        out["description_first_hash"] = first
    out["integrity"] = {
        "score": md.get("integ_score"),
        "flags": md.get("integ_flags") or [],
        **{k[len("integ_"):]: v for k, v in feats.items() if k not in ("integ_score", "integ_flags")},
    }
    return out


def _day_change_from_history(history: list[tuple[int, float]] | None) -> float | None:
    """p_now - p_24h_ago from daily price history (latest point at least ~1 day before the last)."""
    if not history or len(history) < 2:
        return None
    pts = sorted(history)
    t_last, p_now = pts[-1]
    older = [p for t, p in pts if t <= t_last - 0.9 * 86400]
    return round(p_now - older[-1], 6) if older else None


def fetch_integrity_inputs(market: MarketRow) -> tuple[Any, Any, Any]:
    """Network part of the integrity check: ``(history, trades, book)`` (pure fetch; safe to run in a worker thread)."""
    token = market.market_data.get("yes_token_id")
    history = polymarket_api.fetch_price_history(token) if token else None
    trades = polymarket_api.fetch_trades(market.market_id)
    book = polymarket_api.fetch_book(token) if token else None
    return history, trades, book


def _apply_integrity(
    market: MarketRow,
    prior: dict[str, Any] | None,
    coverage: Coverage | None,
    fetched: tuple[Any, Any, Any] | None = None,
) -> None:
    """Fetch history/trades/book (unless ``fetched``), write ``integ_*`` (+ score, flags) into market_data."""
    md = market.market_data
    history, trades, book = fetched if fetched is not None else fetch_integrity_inputs(market)
    _count_signal(coverage, "price_history", history is None)
    _count_signal(coverage, "trades", trades is None)
    _count_signal(coverage, "book", book is None)
    if md.get("day_price_change") is None:
        md["day_price_change"] = _day_change_from_history(history)
        if md["day_price_change"] is None:
            del md["day_price_change"]
    _count_signal(coverage, "day_price_change", md.get("day_price_change") is None)
    feats = market_integrity.features(history, trades, book, md, prior)
    md.update(feats)
    md["integ_score"] = market_integrity.score(feats)
    md["integ_flags"] = market_integrity.flags(feats)


def _record_signal_coverage(vault: ObsidianManager, coverage: Coverage) -> None:
    """One `signal_coverage` ledger row per tick; log.error for a signal missing too often."""
    if not any(total for _, total in coverage.values()):
        return
    for signal, (missing, total) in coverage.items():
        if total > 0 and missing / total > SIGNAL_COVERAGE_ALERT_FRAC:
            log.error(
                "[PHASE 2] signal coverage low: %s missing for %d/%d gated markets",
                signal, missing, total,
            )
    _ledger_safe(
        vault, "_tick", "signal_coverage",
        **{s: {"missing": m, "total": t} for s, (m, t) in coverage.items()},
    )
