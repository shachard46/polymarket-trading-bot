"""Direct client for Polymarket's public APIs (Gamma for market metadata, CLOB for books).

Read-only and unauthenticated. This replaces the old ``poly-scan`` / SQLite scraper: the Hub
now asks Polymarket directly for candidates, fresh prices, executable quotes and resolutions.
``orchestrator.scraper`` is the thin adapter that keeps the phase code's function signatures.

Live-API facts this module relies on (verified read-only, see ``tests/orchestrator/fixtures/polymarket/``):

- ``outcomes`` / ``outcomePrices`` / ``clobTokenIds`` are JSON-*encoded strings* (sometimes lists);
  they are index-aligned. YES/NO order is NOT guaranteed, so tokens are mapped by label.
- ``GET /markets?condition_ids=<0x..>`` hides closed markets unless ``closed=true`` is also passed;
  numeric ids work through ``GET /markets/{id}`` in every state.
- ``GET /markets`` is flagged deprecated by Polymarket; ``GET /markets/keyset`` (cursor paging, same
  filters) is used for candidate listing.
- CLOB ``/book`` returns asks sorted worst-first (descending), so asks are re-sorted here.

Environment: ``GAMMA_BASE_URL`` / ``CLOB_BASE_URL`` (base URL overrides), ``POLYMARKET_API_TIMEOUT_SEC``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests

from orchestrator.util import to_float

from config.trading_constants import (
    LIQ_FRAC,
    MANIP,
    INTEGRITY,
    MAX_DAYS_TO_RESOLUTION,
    MIN_DAYS_TO_RESOLUTION,
    MIN_ORDER_USD,
)

log = logging.getLogger(__name__)

DEFAULT_GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
DEFAULT_CLOB_BASE_URL = "https://clob.polymarket.com"
DEFAULT_DATA_BASE_URL = "https://data-api.polymarket.com/v2"  # v1 was retired; v2 takes condition_id
DEFAULT_TIMEOUT_SEC = 15.0
MAX_ATTEMPTS = 4
BACKOFF_BASE_SEC = 0.5
BACKOFF_CAP_SEC = 8.0
CACHE_TTL_SEC = 60.0
PAGE_SIZE = 100
MAX_PAGES = 20  # keyset pages per listing (PAGE_SIZE rows each); the static pre-gate refills past rejected rows
DEPTH_BAND = 0.02  # executable depth is measured up to best_ask + 2c

MIN_LIQUIDITY_USD = MIN_ORDER_USD / LIQ_FRAC  # same floor as gate.MIN_LIQUIDITY_USD

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_CONDITION_ID_RE = re.compile(r"^0x[0-9a-fA-F]{8,}$")
_UNRESOLVED_UMA = frozenset({"proposed", "disputed", "challenged"})


class PolymarketAPIError(RuntimeError):
    """Transport-level failure (network, timeout, retries exhausted, bad JSON). Infra, not content."""


# ---------------------------------------------------------------------------
# HTTP plumbing: one session, timeouts, retries with backoff, tiny TTL cache
# ---------------------------------------------------------------------------

_session: Any = None
_cache: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def gamma_base_url() -> str:
    return os.environ.get("GAMMA_BASE_URL", DEFAULT_GAMMA_BASE_URL).rstrip("/")


def data_base_url() -> str:
    return os.environ.get("DATA_API_BASE_URL", DEFAULT_DATA_BASE_URL).rstrip("/")


def clob_base_url() -> str:
    return os.environ.get("CLOB_BASE_URL", DEFAULT_CLOB_BASE_URL).rstrip("/")


def _timeout_sec() -> float:
    try:
        value = float(os.environ.get("POLYMARKET_API_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC))
    except ValueError:
        return DEFAULT_TIMEOUT_SEC
    return value if math.isfinite(value) and value > 0 else DEFAULT_TIMEOUT_SEC


def _get_session() -> Any:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"Accept": "application/json", "User-Agent": "polymarket-research-bot"})
    return _session


def set_session(session: Any | None) -> None:
    """Swap the HTTP session (tests); ``None`` restores a fresh real one. Also clears the cache."""
    global _session
    _session = session
    clear_cache()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _retry_delay(attempt: int, response: Any | None) -> float:
    retry_after = getattr(response, "headers", {}).get("Retry-After") if response is not None else None
    try:
        if retry_after is not None:
            return min(max(float(retry_after), 0.0), BACKOFF_CAP_SEC)
    except (TypeError, ValueError):
        pass
    return min(BACKOFF_BASE_SEC * 2**attempt, BACKOFF_CAP_SEC)


def _get_json(url: str, params: dict[str, Any] | None = None, *, cache: bool = True, timeout: float | None = None) -> Any:
    """GET ``url`` and return parsed JSON; retry 429/5xx/transport errors with backoff.

    Raises :class:`PolymarketAPIError` once retries are exhausted or on a non-retryable 4xx.
    """
    key = (url, tuple(sorted((str(k), str(v)) for k, v in (params or {}).items())))
    if cache:
        with _cache_lock:
            hit = _cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < CACHE_TTL_SEC:
            return hit[1]

    last_error = "unknown error"
    for attempt in range(MAX_ATTEMPTS):
        response = None
        try:
            response = _get_session().get(url, params=params, timeout=timeout or _timeout_sec())
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            status = response.status_code
            if status == 200:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise PolymarketAPIError(f"non-JSON response from {url}: {exc}") from exc
                if cache:
                    with _cache_lock:
                        _cache[key] = (time.monotonic(), data)
                return data
            last_error = f"HTTP {status}"
            if status not in _RETRY_STATUSES:
                raise PolymarketAPIError(f"{last_error} from {url}")
        if attempt < MAX_ATTEMPTS - 1:
            delay = _retry_delay(attempt, response)
            log.warning("polymarket api %s (%s); retry %d in %.1fs", url, last_error, attempt + 1, delay)
            _sleep(delay)
    raise PolymarketAPIError(f"{last_error} from {url} after {MAX_ATTEMPTS} attempts")


# ---------------------------------------------------------------------------
# Field parsing
# ---------------------------------------------------------------------------


def _json_list(value: Any) -> list[Any]:
    """Gamma sends some arrays as JSON-encoded strings; accept either form."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


_num = to_float


def _unit(value: Any) -> float | None:
    f = _num(value)
    return f if f is not None and 0.0 < f < 1.0 else None


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00").replace(" ", "T", 1)
    if re.search(r"[+-]\d{2}$", text):  # "+00" -> "+00:00"
        text += ":00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _days_to_resolution(end: datetime, now: datetime | None = None) -> int:
    delta = (end - (now or datetime.now(timezone.utc))).total_seconds() / 86400.0
    return max(1, int(math.ceil(delta)))


def yes_no_indices(raw: dict[str, Any]) -> tuple[int, int] | None:
    """Indices of the YES and NO outcomes, or ``None`` if the market is not a plain Yes/No market."""
    labels = [str(x).strip().lower() for x in _json_list(raw.get("outcomes"))]
    if len(labels) != 2 or set(labels) != {"yes", "no"}:
        return None
    return labels.index("yes"), labels.index("no")


def _mid(bid: float | None, ask: float | None) -> float | None:
    if bid is not None and ask is not None and 0.0 < bid <= ask < 1.0:
        return round((bid + ask) / 2.0, 6)
    return None


def market_row_from_gamma(raw: dict[str, Any], now: datetime | None = None) -> dict[str, Any] | None:
    """Project a Gamma market object into a flat ``MarketRow``-compatible dict.

    ``None`` when the market is not binary Yes/No, lacks token ids, or has no condition id.
    ``yes_bid/yes_ask`` are Gamma's ``bestBid/bestAsk`` (quoted for the YES token); ``no_bid/no_ask``
    are derived from them (``1 - yes_ask`` / ``1 - yes_bid``), the exact complement on a binary book.
    """
    if not isinstance(raw, dict):
        return None
    market_id = raw.get("conditionId")
    if not isinstance(market_id, str) or not market_id:
        return None
    idx = yes_no_indices(raw)
    tokens = [str(t) for t in _json_list(raw.get("clobTokenIds"))]
    if idx is None or len(tokens) != 2:
        return None
    yes_i, no_i = idx
    prices = [_num(p) for p in _json_list(raw.get("outcomePrices"))]
    out_yes = prices[yes_i] if len(prices) == 2 else None
    out_no = prices[no_i] if len(prices) == 2 else None

    yes_bid, yes_ask = _unit(raw.get("bestBid")), _unit(raw.get("bestAsk"))
    mid = _mid(yes_bid, yes_ask)
    yes_price = mid if mid is not None else out_yes
    no_price = None
    if yes_price is not None:
        no_price = round(1.0 - yes_price, 6) if mid is not None else out_no
    no_bid = round(1.0 - yes_ask, 6) if yes_ask is not None else None
    no_ask = round(1.0 - yes_bid, 6) if yes_bid is not None else None

    end_raw = raw.get("endDate") or raw.get("endDateIso")
    end = _parse_dt(end_raw) or _parse_dt(raw.get("endDateIso"))
    events = [e for e in raw.get("events") or [] if isinstance(e, dict)]
    event_id = events[0].get("id") if events else None
    spread = _num(raw.get("spread"))
    if yes_bid is not None and yes_ask is not None:
        spread = round(yes_ask - yes_bid, 6)

    row: dict[str, Any] = {
        "market_id": market_id,
        "gamma_id": str(raw.get("id")) if raw.get("id") is not None else None,
        "question": raw.get("question") or "",
        "title": raw.get("question") or "",
        "description": raw.get("description") or "",
        "yes_price": yes_price,
        "no_price": no_price,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "last_trade_price": _unit(raw.get("lastTradePrice")),
        "midpoint": mid,
        "spread": spread,
        "liquidity": _num(raw.get("liquidityNum")) if raw.get("liquidityNum") is not None else _num(raw.get("liquidity")),
        "volume": _num(raw.get("volumeNum")) if raw.get("volumeNum") is not None else _num(raw.get("volume")),
        "volume_24h": _num(raw.get("volume24hr")),
        "day_price_change": _num(raw.get("oneDayPriceChange")),
        "end_date": end.isoformat() if end else (str(end_raw) if end_raw else None),
        "days_to_resolution": _days_to_resolution(end, now) if end else None,
        "category": raw.get("category") or (events[0].get("category") if events else None),
        "event_id": str(event_id) if event_id is not None else None,
        "slug": raw.get("slug") or None,
        "event_slug": (events[0].get("slug") or None) if events else None,
        "resolution_source": raw.get("resolutionSource") or (events[0].get("resolutionSource") if events else None) or None,
        "yes_token_id": tokens[yes_i],
        "no_token_id": tokens[no_i],
        "created_at": raw.get("createdAt"),
        "uma_resolution_statuses": raw.get("umaResolutionStatuses"),
        "custom_liveness": raw.get("customLiveness"),
        "resolution_mechanism": raw.get("resolutionMechanism"),
        "rewards_min_size": raw.get("rewardsMinSize"),
        "closed": bool(raw.get("closed")),
        "active": bool(raw.get("active", True)),
        "accepting_orders": raw.get("acceptingOrders") is not False and raw.get("enableOrderBook") is not False,
    }
    return {k: v for k, v in row.items() if v is not None}


def _fetch_outstanding_shares(condition_id: str) -> float | None:
    """Shares outstanding per side = market open interest from Data API ``/oi`` (each minted set is 1 YES + 1 NO).

    `None` on any error / unexpected shape / non-positive value.
    """
    try:
        payload = _get_json(
            f"{data_base_url()}/oi",
            {"condition_id": condition_id},
            timeout=MANIP["holders_timeout_sec"],
        )
        entries = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if isinstance(entry, dict) and "value" in entry:
                v = _num(entry.get("value"))
                return v if v is not None and v > 0 else None
    except Exception:  # noqa: BLE001 - advisory; fall back to top-N basis
        return None
    return None


def fetch_holder_concentration(condition_id: str) -> dict[str, Any] | None:
    """Top-holder share per side from the Data API ``/holders``; the worst side wins.

    Returns ``{"top1", "top3", "basis"}`` or `None` on any error, unexpected shape or zero total.
    ``basis="supply"``: shares of total outstanding per side (open interest from ``/oi``); used only when
    that total is available and no top-N sum exceeds it (else the total is deemed unreliable).
    ``basis="top_n"``: fallback, share of the top-N holders' total (overstates concentration).
    Accepts a bare list or ``{"data": [...]}`` of per-token entries.
    """
    try:
        payload = _get_json(
            f"{data_base_url()}/holders",
            {"condition_id": condition_id, "limit": MANIP["holders_limit"]},
            timeout=MANIP["holders_timeout_sec"],
        )
        entries = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            return None
        sides: list[list[float]] = []
        for entry in entries:
            holders = entry.get("holders") if isinstance(entry, dict) else None
            if not isinstance(holders, list):
                return None
            amounts = sorted(
                (a for a in (_num(h.get("amount")) for h in holders if isinstance(h, dict)) if a is not None and a > 0),
                reverse=True,
            )
            if sum(amounts) > 0:
                sides.append(amounts)
        if not sides:
            return None
        supply = _fetch_outstanding_shares(condition_id)
        if supply is not None and any(sum(am) > supply * 1.001 for am in sides):
            supply = None  # top-N exceeding total outstanding: inconsistent, don't trust
        top1 = top3 = 0.0
        for amounts in sides:
            denom = supply if supply is not None else sum(amounts)
            top1 = max(top1, amounts[0] / denom)
            top3 = max(top3, sum(amounts[:3]) / denom)
        return {"top1": round(top1, 6), "top3": round(top3, 6), "basis": "supply" if supply is not None else "top_n"}
    except Exception:  # noqa: BLE001 - advisory signal; never block the pipeline
        return None


def fetch_price_history(token_id: str) -> list[tuple[int, float]] | None:
    """Daily price history of a CLOB token as ``[(t, p)]``; `None` on any error or unexpected shape."""
    try:
        payload = _get_json(
            f"{clob_base_url()}/prices-history",
            {"market": token_id, "interval": "1m", "fidelity": 1440},
            cache=False,
            timeout=INTEGRITY["timeout_sec"],
        )
        history = payload.get("history") if isinstance(payload, dict) else None
        if not isinstance(history, list):
            return None
        out: list[tuple[int, float]] = []
        for point in history:
            t, p = _num(point.get("t")), _num(point.get("p"))
            if t is None or p is None:
                return None
            out.append((int(t), p))
        return out
    except Exception:  # noqa: BLE001 - advisory signal; never block the pipeline
        return None


def fetch_trades(condition_id: str) -> list[dict[str, Any]] | None:
    """Recent trades of a market from the Data API; `None` on any error or unexpected shape.

    Each dict keeps ``proxy_wallet``, ``side``, ``size``, ``price``, ``timestamp`` and ``outcome_index`` (numbers as numbers).
    Accepts a bare list or ``{"data": [...]}``.
    """
    try:
        payload = _get_json(
            f"{data_base_url()}/trades",
            {"condition": condition_id, "limit": INTEGRITY["trades_limit"]},
            cache=False,
            timeout=INTEGRITY["timeout_sec"],
        )
        entries = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            return None
        out: list[dict[str, Any]] = []
        for e in entries:
            if not isinstance(e, dict):
                return None
            size, price, ts, idx = (_num(e.get(k)) for k in ("size", "price", "timestamp", "outcome_index"))
            wallet, side = e.get("proxy_wallet"), e.get("side")
            if size is None or price is None or ts is None or not isinstance(wallet, str) or not isinstance(side, str):
                return None
            out.append(
                {
                    "proxy_wallet": wallet,
                    "side": side,
                    "size": size,
                    "price": price,
                    "timestamp": int(ts),
                    "outcome_index": int(idx) if idx is not None else None,
                }
            )
        return out
    except Exception:  # noqa: BLE001
        return None


def fetch_book(token_id: str) -> dict[str, list[tuple[float, float]]] | None:
    """CLOB order book as ``{"bids": [(p, s)], "asks": [(p, s)]}``, best level first; `None` on any error."""
    try:
        payload = _get_json(
            f"{clob_base_url()}/book", {"token_id": token_id}, cache=False, timeout=INTEGRITY["timeout_sec"]
        )
        if not isinstance(payload, dict):
            return None
        sides: dict[str, list[tuple[float, float]]] = {}
        for name in ("bids", "asks"):
            levels = payload.get(name)
            if not isinstance(levels, list):
                return None
            parsed = []
            for lv in levels:
                p, s = _num(lv.get("price")), _num(lv.get("size"))
                if p is None or s is None:
                    return None
                parsed.append((p, s))
            sides[name] = sorted(parsed, key=lambda x: x[0], reverse=(name == "bids"))
        return sides
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _raw_market(market_id: str, closed_first: bool = False) -> dict[str, Any] | None:
    """Gamma market object by condition id (0x...) or numeric Gamma id; any open/closed state.

    ``closed_first`` tries ``closed=true`` before the open lookup: just-closed markets can still be
    served as a stale open record (closed=false, UMA ``proposed``) by the default query.
    """
    base = gamma_base_url()
    mid = str(market_id).strip()
    if _CONDITION_ID_RE.match(mid):
        # condition_ids hides closed markets unless closed=true is passed too.
        variants = ({"closed": "true"}, {}) if closed_first else ({}, {"closed": "true"})
        for extra in variants:
            data = _get_json(f"{base}/markets", {"condition_ids": mid, **extra})
            if isinstance(data, list) and data and isinstance(data[0], dict):
                return data[0]
        return None
    if mid.isdigit():
        data = _get_json(f"{base}/markets/{mid}")
        return data if isinstance(data, dict) and data.get("id") else None
    return None


def list_candidates(
    limit: int,
    knobs: dict[str, Any] | None = None,
    now: datetime | None = None,
    accept: Callable[[dict[str, Any]], tuple[bool, str]] | None = None,
    rejected: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Open binary Yes/No markets resolving inside the days window with enough liquidity.

    Server-side filters: ``active``, ``closed=false``, ``end_date_min/max`` (``min/max_days_to_resolution``
    from ``knobs`` else the constants), ``liquidity_num_min`` (the gate floor), ordered by liquidity
    descending. Pages through ``/markets/keyset`` until ``limit`` usable rows or the page cap.

    ``accept(row) -> (ok, reason)`` is an optional static pre-gate evaluated on the Gamma row itself
    (no extra API calls). Rejected rows do not count toward ``limit``, so paging continues past them;
    their reasons are tallied into ``rejected`` when given. (Gamma's keyset endpoint has no price or
    spread filter parameter, so this is client-side.)
    """
    knobs = knobs or {}
    min_days = float(knobs.get("min_days_to_resolution") or MIN_DAYS_TO_RESOLUTION)
    max_days = float(knobs.get("max_days_to_resolution") or MAX_DAYS_TO_RESOLUTION)
    now = now or datetime.now(timezone.utc)
    params: dict[str, Any] = {
        "active": "true",
        "closed": "false",
        "limit": PAGE_SIZE,
        "order": "liquidityNum",
        "ascending": "false",
        "liquidity_num_min": MIN_LIQUIDITY_USD,
        "end_date_min": (now + timedelta(days=min_days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_date_max": (now + timedelta(days=max_days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        page_params = dict(params, **({"after_cursor": cursor} if cursor else {}))
        data = _get_json(f"{gamma_base_url()}/markets/keyset", page_params)
        markets = data.get("markets") if isinstance(data, dict) else data
        for raw in markets or []:
            row = market_row_from_gamma(raw, now)
            if row is None or row.get("closed") or not row.get("accepting_orders"):
                continue
            days, liq = row.get("days_to_resolution"), row.get("liquidity")
            if days is None or not (min_days <= days <= max_days):
                continue
            if liq is None or liq < MIN_LIQUIDITY_USD:
                continue
            if accept is not None:
                ok, reason = accept(row)
                if not ok:
                    if rejected is not None:
                        rejected[reason] = rejected.get(reason, 0) + 1
                    continue
            rows.append(row)
            if len(rows) >= limit:
                return rows
        cursor = data.get("next_cursor") if isinstance(data, dict) else None
        if not cursor:
            break
    return rows


def fetch_market(market_id: str) -> dict[str, Any] | None:
    """Fresh row for one market (open or closed); ``None`` if unknown or not binary Yes/No."""
    raw = _raw_market(market_id)
    return market_row_from_gamma(raw) if raw else None


def executable_quote(market_id: str, side: str) -> dict[str, Any] | None:
    """Best ask and ask depth for buying the YES or NO token, from the CLOB book (never cached).

    ``depth_usd_within_2c`` = sum(price * size) over asks priced <= best_ask + 0.02. ``None`` when the
    market is unknown or the book has no asks. Raises :class:`PolymarketAPIError` on transport failure.
    """
    side_key = str(side).strip().upper()
    if side_key not in ("YES", "NO"):
        raise ValueError(f"side must be YES or NO, got {side!r}")
    row = fetch_market(market_id)
    token_id = (row or {}).get("yes_token_id" if side_key == "YES" else "no_token_id")
    if not token_id:
        return None
    book = _get_json(f"{clob_base_url()}/book", {"token_id": token_id}, cache=False)
    if not isinstance(book, dict):
        return None

    def levels(key: str) -> list[tuple[float, float]]:
        out = []
        for lv in book.get(key) or []:
            price, size = _unit(lv.get("price")), _num(lv.get("size"))
            if price is not None and size is not None and size > 0:
                out.append((price, size))
        return out

    asks = sorted(levels("asks"))
    if not asks:
        return None
    best_ask = asks[0][0]
    bids = levels("bids")
    best_bid = max((p for p, _ in bids), default=None)
    depth = sum(p * s for p, s in asks if p <= best_ask + DEPTH_BAND + 1e-9)
    return {
        "ask": best_ask,
        "depth_usd_within_2c": round(depth, 4),
        "spread": round(best_ask - best_bid, 6) if best_bid is not None else None,
        "token_id": token_id,
    }


def resolution(market_id: str) -> dict[str, Any] | None:
    """Resolution of a market, or ``None`` while it is still unresolved.

    A market counts as resolved when Gamma says ``closed`` and ``outcomePrices`` settled at ~1/0.
    ``outcome_yes`` is 1 / 0 for YES / NO won, ``None`` for a closed market without a clean winner
    (void 50/50, or UMA status still ``disputed``); ``status`` says which (``resolved``, ``void``,
    ``disputed``). Works for any market, traded or not.
    """
    raw = _raw_market(market_id, closed_first=True)
    if not raw or not raw.get("closed"):
        return None
    idx = yes_no_indices(raw)
    if idx is None:
        return None
    prices = [_num(p) for p in _json_list(raw.get("outcomePrices"))]
    uma = str(raw.get("umaResolutionStatus") or "").strip().lower()
    resolved_at = _parse_dt(raw.get("closedTime")) or _parse_dt(raw.get("umaEndDate"))

    outcome_yes: int | None = None
    status = "resolved"
    if uma in _UNRESOLVED_UMA:
        status = "disputed" if uma == "disputed" else "closed"
    elif len(prices) == 2 and None not in prices:
        py, pn = prices[idx[0]], prices[idx[1]]
        if py >= 0.99 and pn <= 0.01:
            outcome_yes = 1
        elif py <= 0.01 and pn >= 0.99:
            outcome_yes = 0
        else:
            status = "void"
    else:
        status = "closed"
    return {
        "market_id": str(market_id),
        "status": status,
        "outcome_yes": outcome_yes,
        "raw_outcome": _json_list(raw.get("outcomePrices")) or None,
        "resolved_at": resolved_at.isoformat() if resolved_at else None,
        "resolution_source": raw.get("resolutionSource") or None,
        "uma_status": uma or None,
    }


__all__ = [
    "MIN_LIQUIDITY_USD",
    "PolymarketAPIError",
    "clear_cache",
    "executable_quote",
    "fetch_market",
    "list_candidates",
    "market_row_from_gamma",
    "resolution",
    "set_session",
    "yes_no_indices",
]
