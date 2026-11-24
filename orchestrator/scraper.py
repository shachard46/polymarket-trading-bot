"""Market ingestion adapter over :mod:`orchestrator.polymarket_api` (Gamma + CLOB, no scraper DB).

Keeps the function signatures the phase code was written against (``fetch_target_markets``,
``fetch_market_row``, ``fetch_resolution``, ``select_q``, ``market_data_hydration_error``) while the
data now comes straight from Polymarket's public APIs. Transport failures are logged and converted
to empty results, so a Polymarket outage degrades to "nothing to do this tick" rather than a crash.

Environment variables:

- ``PMBOT_INGEST_LIMIT`` — max candidate markets pulled per ingestion cycle (default ``100``).
- ``GAMMA_BASE_URL`` / ``CLOB_BASE_URL`` / ``POLYMARKET_API_TIMEOUT_SEC`` — see ``polymarket_api``.
"""

from __future__ import annotations

import logging
import math
import os
import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchestrator import polymarket_api
from orchestrator.polymarket_api import PolymarketAPIError

log = logging.getLogger(__name__)

_DEFAULT_INGEST_LIMIT = 100


def _ingest_limit() -> int:
    raw = os.environ.get("PMBOT_INGEST_LIMIT")
    if not raw:
        return _DEFAULT_INGEST_LIMIT
    try:
        return max(1, int(raw))
    except ValueError:
        log.warning("Invalid PMBOT_INGEST_LIMIT=%r; using default", raw)
        return _DEFAULT_INGEST_LIMIT


# ---------------------------------------------------------------------------
# Hub-side payload models
# ---------------------------------------------------------------------------


# Snapshot fields the Orchestrator promises to forward to downstream agents/phases, sourced from
# ``polymarket_api.market_row_from_gamma`` (Gamma ``bestBid``/``bestAsk``/``outcomePrices``/...).
_MARKET_DATA_FIELDS: tuple[str, ...] = (
    "yes_price",
    "no_price",
    "yes_bid",
    "yes_ask",
    "no_bid",
    "no_ask",
    "volume",
    "volume_24h",
    "day_price_change",
    "liquidity",
    "last_trade_price",
    "midpoint",
    "spread",
    "end_date",
    "days_to_resolution",
    "category",
    "event_id",
    "slug",
    "event_slug",
    "neg_risk",
    "resolution_source",
    "yes_token_id",
    "no_token_id",
    # market-integrity inputs (Hub-side only; never sent to price-blind agents)
    "created_at",
    "uma_resolution_statuses",
    "custom_liveness",
    "resolution_mechanism",
    "rewards_min_size",
    "description",
)


class MarketRow(BaseModel):
    """Hub-validated row carrying every field downstream spokes need.

    Built from a ``polymarket_api`` row. The Hub reads ``market_data`` (q, V, L, ...); price-blind
    agents only receive the text fields and a whitelisted subset of ``market_data``.
    """

    model_config = ConfigDict(extra="forbid")

    market_id: str
    market_title: str
    market_description: str = ""
    market_data: dict[str, Any] = Field(default_factory=dict)


# Fields the Hub uses for implied price (q), after the yes_bid/yes_ask mid, in priority order.
Q_SOURCE_FIELDS: tuple[str, ...] = ("yes_price", "last_trade_price", "midpoint")

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _days_to_resolution(end_date: datetime) -> int:
    delta_days = (end_date - datetime.now(timezone.utc)).total_seconds() / 86400.0
    return max(1, int(math.ceil(delta_days)))


def _market_snapshot_from_scraper_row(row: dict[str, Any]) -> dict[str, Any]:
    """Project a ``polymarket_api`` row into agent-facing ``market_data``."""
    market_data: dict[str, Any] = {
        key: row[key] for key in _MARKET_DATA_FIELDS if key in row and row[key] is not None
    }
    if "end_date" not in market_data:
        for key in ("endDateIso", "endDate"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                market_data["end_date"] = value.strip()
                break
    if "days_to_resolution" not in market_data and "end_date" in market_data:
        parsed_end = _parse_iso_datetime(market_data["end_date"])
        if parsed_end is not None:
            market_data["days_to_resolution"] = _days_to_resolution(parsed_end)
    return market_data


def _is_unit_price(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0.0 < float(value) < 1.0
    )


def select_q(row: dict[str, Any] | None) -> float | None:
    """Implied price q (YES-first): mid of ``yes_bid``/``yes_ask``, else the first of
    ``yes_price``, ``last_trade_price``, ``midpoint`` strictly in (0, 1)."""
    data = row or {}
    bid, ask = data.get("yes_bid"), data.get("yes_ask")
    if _is_unit_price(bid) and _is_unit_price(ask) and float(bid) <= float(ask):
        return (float(bid) + float(ask)) / 2.0
    for key in Q_SOURCE_FIELDS:
        value = data.get(key)
        if _is_unit_price(value):
            return float(value)
    return None


def _missing_pricing_error(data: dict[str, Any]) -> str | None:
    if select_q(data) is not None:
        return None
    return (
        "missing market pricing data: no yes_bid/yes_ask, yes_price, last_trade_price, or "
        "midpoint in (0, 1) (the market may be closed or have an empty book)"
    )


def market_data_pricing_error(market_data: dict[str, Any] | None) -> str | None:
    """Return a reason when implied price (q) cannot be derived, else ``None``."""
    return _missing_pricing_error(market_data or {})


def _is_positive_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0.0
    )


def market_data_hydration_error(market_data: dict[str, Any] | None) -> str | None:
    """Return a reason when snapshot is unusable for execution (phase 4), else ``None``.

    ``volume`` and ``liquidity`` must be present and strictly positive: the allocation skill divides
    by ``log1p(V)`` / ``log1p(L)``, so a zero value would crash. Treat it as unusable for this tick.
    """
    data = market_data or {}
    pricing_err = _missing_pricing_error(data)
    if pricing_err:
        return pricing_err
    unusable = [key for key in ("volume", "liquidity") if not _is_positive_number(data.get(key))]
    if unusable:
        details = ", ".join(f"{key}={data.get(key)!r}" for key in unusable)
        return (
            f"market snapshot has missing or non-positive {', '.join(unusable)} ({details}); "
            "will retry when Polymarket reports them"
        )
    return None


def _market_row_from_scraper(row: dict[str, Any]) -> MarketRow | None:
    """Project a ``polymarket_api`` row dict into a validated :class:`MarketRow`.

    Returns ``None`` if the row lacks a usable ``market_id`` (condition id) or ``question``.
    """
    market_id = row.get("market_id")
    if isinstance(market_id, int) and not isinstance(market_id, bool):
        market_id = str(market_id)
    if not isinstance(market_id, str) or not market_id:
        return None
    if not _SAFE_ID_RE.match(market_id) or ".." in market_id:
        log.warning("Dropping market with unsafe market_id %r", market_id)
        return None

    title = row.get("question") or row.get("market_title") or ""
    if not isinstance(title, str) or not title.strip():
        # No human-readable title means the Briefer would hallucinate against an empty string.
        return None

    description = row.get("description") or row.get("market_description") or ""
    if not isinstance(description, str):
        description = str(description)

    return MarketRow(
        market_id=market_id,
        market_title=title,
        market_description=description,
        market_data=_market_snapshot_from_scraper_row(row),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_target_markets(
    knobs: dict[str, Any] | None = None, vault: Any = None
) -> list[MarketRow]:
    """Return validated candidate rows (open binary markets inside the days window, liquid enough).

    ``knobs`` is the directives-derived mapping (``extract_filter_directives``) so the server-side
    window follows the Overseer-tunable ``min/max_days_to_resolution``. Limit is
    ``PMBOT_INGEST_LIMIT``. Phase 2 still applies the full deterministic gate.
    With a ``vault``, markets Phase 2 would skip anyway (inactive, retired, backing off:
    ``gate.handled_reason``) do not use slots either.
    """
    from orchestrator import gate  # noqa: PLC0415 - lazy: gate imports this module

    rejected: dict[str, int] = {}
    now = datetime.now(timezone.utc)

    def accept(row: dict[str, Any]) -> tuple[bool, str]:
        if vault is not None:
            _, handled = gate.handled_reason(vault, str(row.get("market_id")), now)
            if handled:
                return False, handled
        return gate.eligibility(row, knobs)

    try:
        # Static pre-gate on the Gamma row (price band, wide spread, ... : no extra API calls) so rows
        # the gate would reject anyway do not eat the ingest slots; paging refills up to the limit.
        data = polymarket_api.list_candidates(
            _ingest_limit(), knobs, accept=accept, rejected=rejected
        )
    except PolymarketAPIError as exc:
        log.warning("Polymarket candidate listing failed: %s", exc)
        return []
    if rejected:
        log.info(
            "Ingest pre-gate skipped %d rows (not ledgered; the gate or Phase 2 would skip them): %s",
            sum(rejected.values()), dict(sorted(rejected.items())),
        )

    rows: list[MarketRow] = []
    for raw in data:
        row = _market_row_from_scraper(raw)
        if row is not None:
            rows.append(row)
        else:
            log.warning(
                "Dropped candidate (market_id=%r): missing/unsafe id or title", raw.get("market_id")
            )
    log.info("Ingested %d candidate markets", len(rows))
    return rows


def fetch_market_row(market_id: str) -> MarketRow | None:
    """Return a :class:`MarketRow` with fresh prices for one market (open or closed)."""
    if not isinstance(market_id, str) or not _SAFE_ID_RE.match(market_id) or ".." in market_id:
        return None
    try:
        market = polymarket_api.fetch_market(market_id)
    except PolymarketAPIError as exc:
        log.warning("Polymarket market fetch failed for %s: %s", market_id, exc)
        return None
    return _market_row_from_scraper(market) if market else None


def fetch_resolution(market_id: str) -> dict[str, Any] | None:
    """Return resolution metadata for ``market_id`` once it has resolved, else ``None``.

    ``outcome`` is 1 (YES won) / 0 (NO won) / ``None`` (void); disputed markets return ``None`` so the
    caller waits. Works for any market id, including ones that were only ever gate-rejected.
    """
    if not isinstance(market_id, str) or not _SAFE_ID_RE.match(market_id) or ".." in market_id:
        return None
    try:
        res = polymarket_api.resolution(market_id)
    except PolymarketAPIError as exc:
        log.warning("Polymarket resolution fetch failed for %s: %s", market_id, exc)
        return None
    if res is None or res.get("status") == "disputed":
        return None
    return {**res, "outcome": res.get("outcome_yes")}


__all__ = [
    "MarketRow",
    "Q_SOURCE_FIELDS",
    "market_data_hydration_error",
    "market_data_pricing_error",
    "select_q",
    "fetch_target_markets",
    "fetch_market_row",
    "fetch_resolution",
]
