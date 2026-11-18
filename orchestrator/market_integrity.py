"""Deterministic market-integrity features and score (detect deceptive small markets).

Pure functions only (no network, no vault). ``features`` turns price history, taker trades, the YES
book and flat market data into ``integ_*`` values; any input that is None (or any feature that
cannot be computed) yields None for the affected features. Missing data never rejects (see
``gate.eligibility``). ``score`` is record-only: 1 minus the mean of soft flags in [0, 1].
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from config.trading_constants import INTEGRITY

_DAY = 86400.0
_BAND = 0.02  # depth band around mid (2c)


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _secs(value: Any) -> float | None:
    t = _num(value)
    if t is None:
        return None
    return t / 1000.0 if t > 1e11 else t


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def _ramp_features(history: Sequence[Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {"integ_ramp_7d": None, "integ_ramp_30d": None, "integ_ramp_step_share": None}
    if not history:
        return out
    pts = []
    for item in history:
        try:
            t, p = _secs(item[0]), _num(item[1])
        except (TypeError, IndexError, KeyError):
            continue
        if t is not None and p is not None:
            pts.append((t, p))
    pts.sort()
    if len(pts) < 2:
        return out
    t_last, p_now = pts[-1]
    out["integ_ramp_30d"] = abs(p_now - pts[0][1])
    older = [p for t, p in pts if t <= t_last - 7 * _DAY]
    if older:
        out["integ_ramp_7d"] = abs(p_now - older[-1])
    net = p_now - pts[0][1]
    if abs(net) > 1e-9:
        sign = 1.0 if net > 0 else -1.0
        steps = sorted((max(0.0, sign * (b[1] - a[1])) for a, b in zip(pts, pts[1:])), reverse=True)
        out["integ_ramp_step_share"] = _clip(sum(steps[:2]) / abs(net))
    return out


def _trade_features(trades: Sequence[Mapping[str, Any]] | None, now: float) -> dict[str, Any]:
    keys = (
        "integ_vol_30d_usd", "integ_n_trades_30d", "integ_n_wallets_30d", "integ_top1_wallet_share",
        "integ_top3_wallet_share", "integ_two_sided_wallet_share", "integ_days_since_trade",
    )
    out: dict[str, Any] = dict.fromkeys(keys)
    if trades is None:
        return out
    cutoff = now - INTEGRITY["history_days"] * _DAY
    vol_by: dict[str, float] = {}
    sides: dict[str, set[str]] = {}
    total = 0.0
    n = 0
    last_ts: float | None = None
    for tr in trades:
        if not isinstance(tr, Mapping):
            continue
        ts = _secs(tr.get("timestamp"))
        if ts is None:
            continue
        last_ts = ts if last_ts is None else max(last_ts, ts)
        size, price = _num(tr.get("size")), _num(tr.get("price"))
        if ts < cutoff or size is None or price is None:
            continue
        v = size * price
        w = str(tr.get("proxy_wallet") or "")
        vol_by[w] = vol_by.get(w, 0.0) + v
        sides.setdefault(w, set()).add(str(tr.get("side") or "").upper())
        total += v
        n += 1
    out["integ_vol_30d_usd"] = total
    out["integ_n_trades_30d"] = n
    out["integ_n_wallets_30d"] = len(vol_by)
    if last_ts is not None:
        out["integ_days_since_trade"] = max(0.0, (now - last_ts) / _DAY)
    if total > 0:
        shares = sorted((v / total for v in vol_by.values()), reverse=True)
        out["integ_top1_wallet_share"] = shares[0]
        out["integ_top3_wallet_share"] = sum(shares[:3])
        both = sum(v for w, v in vol_by.items() if {"BUY", "SELL"} <= sides[w])
        out["integ_two_sided_wallet_share"] = both / total
    return out


def _levels(raw: Any) -> list[tuple[float, float]]:
    res = []
    for lv in raw or []:
        try:
            p, s = _num(lv[0]), _num(lv[1])
        except (TypeError, IndexError, KeyError):
            continue
        if p is not None and s is not None:
            res.append((p, s))
    return res


def _book_features(book: Mapping[str, Any] | None, md: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"integ_depth_2c_usd": None, "integ_liq_to_depth": None, "integ_ask_wall_share": None}
    if not isinstance(book, Mapping):
        return out
    bids, asks = _levels(book.get("bids")), _levels(book.get("asks"))
    if not bids and not asks:
        return out
    if bids and asks:
        mid = (max(p for p, _ in bids) + min(p for p, _ in asks)) / 2.0
    else:
        mid = _mid(md)
        if mid is None:
            return out
    near_b = [(p, s) for p, s in bids if p >= mid - _BAND]
    near_a = [(p, s) for p, s in asks if p <= mid + _BAND]
    depth = sum(p * s for p, s in near_b + near_a)
    out["integ_depth_2c_usd"] = depth
    liq = _num(md.get("liquidity"))
    if liq is not None and depth > 0:
        out["integ_liq_to_depth"] = liq / depth
    ask_depth = sum(p * s for p, s in near_a)
    if ask_depth > 0:
        out["integ_ask_wall_share"] = max(p * s for p, s in near_a) / ask_depth
    return out


def _mid(md: Mapping[str, Any]) -> float | None:
    bid, ask = _num(md.get("yes_bid")), _num(md.get("yes_ask"))
    return (bid + ask) / 2.0 if bid is not None and ask is not None else None


def _parse_dt(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _secs(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def _n_disputes(raw: Any) -> int | None:
    if isinstance(raw, str):
        if not raw.strip():
            return None
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, (list, tuple)):
        return None
    return sum(1 for s in raw if str(s).strip().lower() == "disputed")


def features(
    history: Sequence[Any] | None,
    trades: Sequence[Mapping[str, Any]] | None,
    book: Mapping[str, Any] | None,
    md: Mapping[str, Any] | None,
    prior_filter: Mapping[str, Any] | None,
    now: float | None = None,
) -> dict[str, Any]:
    """All ``integ_*`` features; uncomputable ones are None. ``now`` is unix seconds."""
    md = md or {}
    now = time.time() if now is None else float(now)
    out: dict[str, Any] = {}
    out.update(_ramp_features(history))
    out.update(_trade_features(trades, now))
    out.update(_book_features(book, md))

    ramp, vol = out["integ_ramp_30d"], out["integ_vol_30d_usd"]
    out["integ_move_per_kusd"] = ramp / (vol / 1000.0) if ramp is not None and vol else None
    vol24 = _num(md.get("volume_24h"))
    out["integ_vol_spike"] = vol24 / (vol / 30.0) if vol24 is not None and vol else None
    last, mid = _num(md.get("last_trade_price")), _mid(md)
    out["integ_last_vs_mid"] = abs(last - mid) if last is not None and mid is not None else None

    rew = _num(md.get("rewards_min_size"))
    out["integ_rewards_on"] = None if rew is None else rew > 0

    desc = md.get("description")
    h = None
    if isinstance(desc, str) and desc.strip():
        h = hashlib.sha256(" ".join(desc.split()).encode("utf-8")).hexdigest()[:16]
    out["integ_description_hash"] = h
    first = (prior_filter or {}).get("description_first_hash")
    out["integ_rules_changed"] = bool(first and h and first != h)

    out["integ_n_disputes"] = _n_disputes(md.get("uma_resolution_statuses"))
    out["integ_custom_liveness"] = md.get("custom_liveness")
    out["integ_resolution_mechanism"] = md.get("resolution_mechanism")
    created = _parse_dt(md.get("created_at"))
    out["integ_age_days"] = max(0.0, (now - created) / _DAY) if created is not None else None
    return out


def _soft_flags(f: Mapping[str, Any]) -> dict[str, float | None]:
    def ratio(key: str, alarm: str) -> float | None:
        v = _num(f.get(key))
        return None if v is None else _clip(v / INTEGRITY[alarm])

    age = _num(f.get("integ_age_days"))
    return {
        "ramp": ratio("integ_ramp_30d", "alarm_ramp_30d"),
        "top1_wallet": ratio("integ_top1_wallet_share", "alarm_top1_share"),
        "two_sided_wash": ratio("integ_two_sided_wallet_share", "alarm_two_sided_share"),
        "stale": ratio("integ_days_since_trade", "alarm_days_since_trade"),
        "vol_spike": ratio("integ_vol_spike", "alarm_vol_spike"),
        "liq_to_depth": ratio("integ_liq_to_depth", "alarm_liq_to_depth"),
        "ask_wall": ratio("integ_ask_wall_share", "alarm_ask_wall_share"),
        "young": None if age is None else _clip(1.0 - age / INTEGRITY["young_age_days"]),
    }


def score(f: Mapping[str, Any] | None) -> float | None:
    """Integrity score in [0, 1] (1 = clean); None when no soft flag is computable."""
    vals = [v for v in _soft_flags(f or {}).values() if v is not None]
    if not vals:
        return None
    return round(1.0 - sum(vals) / len(vals), 4)


def flags(f: Mapping[str, Any] | None) -> list[str]:
    """Names of soft flags at or above 0.5."""
    return [k for k, v in _soft_flags(f or {}).items() if v is not None and v >= 0.5]


__all__ = ["features", "flags", "score"]
