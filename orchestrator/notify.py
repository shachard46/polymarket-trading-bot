"""Concierge outbox: offers and daily digests the Hub writes for the user-facing front-end.

The Hub writes; the concierge (OpenClaw / Discord) only reads these files and writes votes to
``06_Concierge/inbox/``. Nothing here decides anything and nothing here may break a tick.

Env: ``CONCIERGE_APPROVAL`` (0/1, default 0), ``CONCIERGE_POLL_SEC`` (300),
``CONCIERGE_OFFER_TTL_SEC`` (14400), ``DISCORD_WEBHOOK_URL`` (optional).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from obsidian_utils import ObsidianManager

log = logging.getLogger(__name__)

_tick_id: str | None = None


def approval_enabled() -> bool:
    return os.environ.get("CONCIERGE_APPROVAL", "0").strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(float(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def poll_sec() -> int:
    return _env_int("CONCIERGE_POLL_SEC", 300)


def offer_ttl_sec() -> int:
    return _env_int("CONCIERGE_OFFER_TTL_SEC", 14400)


def approve_directives_enabled() -> bool:
    return os.environ.get("CONCIERGE_APPROVE_DIRECTIVES", "0").strip().lower() in ("1", "true", "yes", "on")


def directive_ttl_sec() -> int:
    return _env_int("CONCIERGE_DIRECTIVE_TTL_SEC", 172800)


DIRECTIVE_ID_PREFIX = "directive-"


def is_directive_id(value: Any) -> bool:
    return str(value or "").startswith(DIRECTIVE_ID_PREFIX)


def webhook_url() -> str | None:
    return os.environ.get("DISCORD_WEBHOOK_URL", "").strip() or None


def set_tick(tick_id: str | None) -> None:
    global _tick_id
    _tick_id = tick_id


def current_tick() -> str:
    return _tick_id or datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def concierge_dir(vault: ObsidianManager) -> Path:
    return vault._dirs["concierge"]


def outbox_dir(vault: ObsidianManager) -> Path:
    path = concierge_dir(vault) / "outbox"
    path.mkdir(parents=True, exist_ok=True)
    return path


def inbox_dir(vault: ObsidianManager) -> Path:
    path = concierge_dir(vault) / "inbox"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def move_to_done(path: Path) -> None:
    """Move a processed outbox/inbox file into the sibling ``_done/`` directory."""
    try:
        done = path.parent / "_done"
        done.mkdir(exist_ok=True)
        os.replace(path, done / path.name)
    except OSError:
        log.exception("could not move %s to _done", path)


def parse_ts(value: Any) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- offers


def _evidence_summary(vault: ObsidianManager, market_id: str, limit: int = 500) -> str | None:
    try:
        path = vault.market_file(market_id, "active")
        if path is None:
            return None
        from orchestrator.research import split_yaml_frontmatter_markdown

        _, body = split_yaml_frontmatter_markdown(path.read_text(encoding="utf-8"))
        text = " ".join(body.replace("#", " ").split())
        return text[:limit] or None
    except Exception:  # noqa: BLE001 - a summary is optional
        return None


def offer_path(vault: ObsidianManager, offer_id: str) -> Path:
    return outbox_dir(vault) / f"offer-{offer_id}.json"


def build_offer(
    vault: ObsidianManager,
    trade: dict[str, Any],
    market_data: dict[str, Any],
    *,
    mode: str,
    resume: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Offer dict from the Phase 4 trade record (``p4_execution._trade_record``)."""
    now = now or _utc_now()
    market_id = trade["market_id"]
    offer_id = f"{market_id}-{current_tick()}"
    slug = market_data.get("slug")
    offer: dict[str, Any] = {
        "offer_id": offer_id,
        "market_id": market_id,
        "mode": mode,  # informational (already executed / flag 0) | approval (pending a vote)
        "status": "pending" if mode == "approval" else "informational",
        "question": market_data.get("question") or market_data.get("title"),
        "url": market_data.get("url") or (f"https://polymarket.com/market/{slug}" if slug else None),
        "side": trade.get("side"),
        "p": trade.get("p"),
        "p_low": trade.get("p_low"),
        "p_high": trade.get("p_high"),
        "q_entry": trade.get("q_entry"),
        "edge": trade.get("score"),
        "allocation_usd": trade.get("allocation_usd"),
        "shares": trade.get("shares"),
        "price": trade.get("price"),
        "ask": trade.get("ask"),
        "depth_usd": trade.get("depth_usd"),
        "evidence_summary": _evidence_summary(vault, market_id),
        "manipulation_risk": market_data.get("manipulation_risk") or "unknown",
        "integ_score": market_data.get("integ_score"),
        "integ_flags": market_data.get("integ_flags"),
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=offer_ttl_sec())).isoformat(),
    }
    if resume is not None:
        offer["resume"] = resume
    return offer


def write_offer(vault: ObsidianManager, offer: dict[str, Any]) -> Path:
    return _write_json(offer_path(vault, offer["offer_id"]), offer)


def offer_message(offer: dict[str, Any]) -> str:
    head = "BUY OFFER (vote needed)" if offer.get("mode") == "approval" else "Trade placed"
    return (
        f"{head}: {offer.get('question') or offer.get('market_id')}\n"
        f"{offer.get('side')} ${offer.get('allocation_usd')} @ ask {offer.get('ask')} | "
        f"p={offer.get('p')} q={offer.get('q_entry')} edge={offer.get('edge')}\n"
        f"offer_id={offer.get('offer_id')} expires {offer.get('expires_at')}"
    )


def post_discord(text: str) -> bool:
    """POST ``text`` to DISCORD_WEBHOOK_URL if set; failures are logged, never raised."""
    url = webhook_url()
    if not url:
        return False
    try:
        import requests

        resp = requests.post(url, json={"content": text[:1900]}, timeout=5)
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("discord webhook failed: %s", exc)
        return False


def emit_offer(
    vault: ObsidianManager,
    trade: dict[str, Any],
    market_data: dict[str, Any],
    *,
    mode: str,
    resume: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Write the outbox offer file (+ Discord ping). Never raises; ``None`` on failure."""
    try:
        offer = build_offer(vault, trade, market_data, mode=mode, resume=resume)
        write_offer(vault, offer)
    except Exception:  # noqa: BLE001
        log.exception("offer write failed for %s", trade.get("market_id"))
        return None
    post_discord(offer_message(offer))
    return offer


# ---------------------------------------------------------------- digest


def digest_path(vault: ObsidianManager, day: str) -> Path:
    return outbox_dir(vault) / f"digest-{day}.json"


def _offers_since(vault: ObsidianManager, cutoff: datetime) -> int:
    n = 0
    out = outbox_dir(vault)
    for path in list(out.glob("offer-*.json")) + list((out / "_done").glob("offer-*.json")):
        data = read_json(path) or {}
        ts = parse_ts(data.get("created_at"))
        if ts and ts >= cutoff:
            n += 1
    return n


def build_digest(vault: ObsidianManager, now: datetime | None = None) -> dict[str, Any]:
    from orchestrator import ledger, stats

    now = now or _utc_now()
    rows = list(ledger.iter_rows(vault))
    s = stats.compute_stats(rows)
    day_ago, week_ago = now - timedelta(days=1), now - timedelta(days=7)

    def since(row: dict[str, Any], cutoff: datetime) -> bool:
        ts = parse_ts(row.get("ts"))
        return bool(ts and ts >= cutoff)

    integ_rejects: dict[str, int] = {}
    for r in rows:
        if r.get("stage") == "gate_reject" and r.get("reason") in stats._INTEG_REASONS and since(r, day_ago):
            integ_rejects[r["reason"]] = integ_rejects.get(r["reason"], 0) + 1
    proposals = sorted(p.name for p in outbox_dir(vault).glob("directive-v*.md"))
    return {
        "date": now.strftime("%Y-%m-%d"),
        "generated_at": now.isoformat(),
        "realised_pnl_after_tax": s["pnl_after_tax"],
        "counterfactual_pnl_after_tax": s["counterfactual_after_tax"]["after_tax"],
        "brier_p": s.get("brier_p"),
        "brier_q": s.get("brier_q"),
        "n_resolved": s["n_resolved"],
        "resolved_this_week": sum(1 for r in rows if r.get("stage") == ledger.RESOLUTION_STAGE and since(r, week_ago)),
        "new_trades": sum(1 for r in rows if r.get("stage") == "traded" and since(r, day_ago)),
        "new_offers": _offers_since(vault, day_ago),
        "integrity_rejects": integ_rejects,
        "pending_directive_proposal": proposals[-1] if proposals else None,
    }


def emit_digest_if_due(vault: ObsidianManager, now: datetime | None = None) -> Path | None:
    """Write ``digest-<UTC date>.json`` once per UTC day. Never raises."""
    try:
        now = now or _utc_now()
        path = digest_path(vault, now.strftime("%Y-%m-%d"))
        if path.exists():
            return None
        digest = build_digest(vault, now)
        _write_json(path, digest)
    except Exception:  # noqa: BLE001
        log.exception("digest failed")
        return None
    post_discord(
        f"Daily digest {digest['date']}: P&L ${digest['realised_pnl_after_tax']:.2f} "
        f"(counterfactual ${digest['counterfactual_pnl_after_tax']:.2f}), resolved {digest['n_resolved']}, "
        f"new trades {digest['new_trades']}, offers {digest['new_offers']}"
    )
    return path


__all__ = [
    "approval_enabled", "approve_directives_enabled", "directive_ttl_sec", "is_directive_id", "build_digest", "build_offer", "current_tick", "emit_digest_if_due", "emit_offer",
    "inbox_dir", "move_to_done", "offer_message", "offer_path", "offer_ttl_sec", "outbox_dir",
    "poll_sec", "post_discord", "set_tick", "write_offer",
]
