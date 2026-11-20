"""Phase 5 - Resolution & post-mortem."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from obsidian_utils import ObsidianManager, VaultWriteError, read_json, write_json_atomic
from orchestrator.gate import parse_ts
from orchestrator import failure_tags, ledger, scraper, stats
from orchestrator.phases.agent_call import run_structured_agent
from orchestrator.phases.common import (
    _as_float,
    _filter_integrity_fields,
    _filter_manipulation_risk,
    _ledger_model_fields,
    _ledger_safe,
    log,
)
from orchestrator.runner import AgentRunner, spawn_agent
from orchestrator.state import flag_inactive, is_inactive, market_quarantine

MAX_POST_MORTEM_ATTEMPTS = 3



def phase5_resolution_and_post_mortem(
    vault: ObsidianManager,
    runner: AgentRunner = spawn_agent,
) -> None:
    """Resolved markets → ``04_Post_Mortems/`` → analyst appends analysis."""
    log.info("[PHASE 5] Resolution & post-mortem")
    for trade_path in vault.iter_open_trades():
        market_id = trade_path.stem
        trade = vault.read_trade_log_dict(market_id)
        if trade is not None and is_inactive(trade):
            log.info("[PHASE 5] skip %s: trade log marked inactive", market_id)
            continue
        with market_quarantine(vault, market_id, "phase5"):
            _resolve_market(vault, runner, market_id)
    _resolve_ledger_markets(vault)


SWEEP_STATE_FILE = "ledger_sweep_state.json"
_TIER_BY_STAGE = {"gate_reject": 2, "triage_reject": 1}  # everything else (traded, below_edge, ...) = 0


def _sweep_state_path(vault: ObsidianManager):
    return vault._dirs["system"] / SWEEP_STATE_FILE


def _market_sweep_info(vault: ObsidianManager) -> dict[str, dict[str, Any]]:
    """Per unresolved market: best (lowest) priority tier and end_date string from its ledger rows."""
    info: dict[str, dict[str, Any]] = {}
    for row in ledger.iter_rows(vault):
        mid = row.get("market_id")
        stage = row.get("stage")
        if not mid or stage in ledger.NON_MARKET_STAGES or stage == ledger.POST_MORTEM_STAGE:
            continue
        rec = info.setdefault(mid, {"tier": 99, "end": None, "resolved": False})
        if stage == ledger.RESOLUTION_STAGE:
            rec["resolved"] = True
            continue
        rec["tier"] = min(rec["tier"], _TIER_BY_STAGE.get(stage, 0))
        if row.get("end_date"):
            rec["end"] = row["end_date"]
    return {m: r for m, r in info.items() if not r["resolved"]}


def _end_date_of(vault: ObsidianManager, market_id: str, rec: dict[str, Any]) -> datetime | None:
    end = parse_ts(rec.get("end"))
    if end is not None:
        return end
    try:
        filt = vault.read_filter_log(market_id) or {}
    except Exception:  # noqa: BLE001
        return None
    md = filt.get("market_data") if isinstance(filt.get("market_data"), dict) else {}
    return parse_ts(filt.get("end_date") or md.get("end_date"))


def ledger_sweep_order(
    vault: ObsidianManager, now: datetime | None = None
) -> tuple[list[str], dict[str, int]]:
    """Markets worth checking now, plus counts.

    Due markets (end_date passed) first: tier, then oldest end_date. Unknown end_date after them,
    least-recently-checked first. Future-dated markets are skipped.
    """
    now = now or datetime.now(timezone.utc)
    info = _market_sweep_info(vault)
    state = read_json(_sweep_state_path(vault))
    last = state.get("last_checked") if isinstance(state, dict) else None
    last = last if isinstance(last, dict) else {}
    due: list[tuple[int, datetime, str]] = []
    unknown: list[tuple[float, str]] = []
    future = 0
    for mid, rec in info.items():
        end = _end_date_of(vault, mid, rec)
        if end is None:
            unknown.append((_as_float(last.get(mid)) or 0.0, mid))
        elif end > now:
            future += 1
        else:
            due.append((rec["tier"], end, mid))
    due.sort(key=lambda t: (t[0], t[1], t[2]))
    unknown.sort()
    order = [m for _, _, m in due] + [m for _, m in unknown]
    return order, {
        "unresolved": len(info), "due": len(due), "skipped_future": future, "unknown_end": len(unknown),
    }


def _resolve_ledger_markets(vault: ObsidianManager, now: datetime | None = None) -> None:
    """Record resolutions for due unresolved ledger markets (capped per tick)."""
    try:
        limit = int(os.environ.get("PMBOT_LEDGER_RESOLVE_LIMIT", "100"))
    except ValueError:
        limit = 100
    now = now or datetime.now(timezone.utc)
    order, counts = ledger_sweep_order(vault, now)
    batch = order[: max(limit, 0)]
    checked = resolved = 0
    stamps: dict[str, float] = {}
    for market_id in batch:
        checked += 1
        stamps[market_id] = now.timestamp()
        try:
            resolution = scraper.fetch_resolution(market_id)
            outcome_yes = ledger.outcome_yes_from_resolution(resolution)
            if outcome_yes is None:
                continue
            filt = vault.read_filter_log(market_id)
            ledger.record_resolution(
                vault, market_id, outcome_yes, (resolution or {}).get("raw_outcome"), None,
                manipulation_risk=_filter_manipulation_risk(filt),
                **_filter_integrity_fields(filt),
            )
            resolved += 1
        except Exception:  # noqa: BLE001 - one bad market must not stop the sweep
            log.exception("[PHASE 5] ledger resolution failed for %s", market_id)
    if stamps:
        try:
            state = read_json(_sweep_state_path(vault))
            prev = state.get("last_checked") if isinstance(state, dict) else None
            merged = {**(prev if isinstance(prev, dict) else {}), **stamps}
            live = set(counts and ledger.unresolved_market_ids(vault))
            merged = {m: t for m, t in merged.items() if m in live}
            write_json_atomic(_sweep_state_path(vault), {"last_checked": merged})
        except Exception:  # noqa: BLE001 - rotation state is advisory
            log.exception("[PHASE 5] ledger sweep state write failed")
    log.info(
        "[PHASE 5] ledger sweep: unresolved=%d due=%d checked=%d resolved=%d skipped_future=%d unknown_end=%d",
        counts["unresolved"], counts["due"], checked, resolved, counts["skipped_future"], counts["unknown_end"],
    )


def _post_mortem_failed(
    vault: ObsidianManager,
    market_id: str,
    reason: str,
    payload: dict[str, Any],
) -> None:
    """Count the failed attempt on the trade log; flag it inactive once the cap is hit."""
    trade = vault.read_trade_log_dict(market_id) or {}
    try:
        attempts = int(trade.get("post_mortem_attempts") or 0) + 1
    except (TypeError, ValueError):
        attempts = 1
    vault.patch_json(market_id, "trades", {"post_mortem_attempts": attempts})
    if attempts >= MAX_POST_MORTEM_ATTEMPTS:
        flag_inactive(vault, market_id, "phase5", reason, payload)
    else:
        log.warning(
            "[PHASE 5] post-mortem attempt %d/%d failed for %s (retry next tick): %s",
            attempts,
            MAX_POST_MORTEM_ATTEMPTS,
            market_id,
            reason,
        )


CITED_EVIDENCE_MAX_LINES = 40
CITED_EVIDENCE_MAX_CHARS = 8000
CRUX_TABLE_KEYS = ("crux_id", "role", "question", "status", "missing")


def _crux_table_for_pm(crux_meta: Any) -> list[dict[str, Any]] | None:
    cruxes = crux_meta.get("cruxes") if isinstance(crux_meta, dict) else None
    if not isinstance(cruxes, list):
        return None
    rows = [{k: c.get(k) for k in CRUX_TABLE_KEYS} for c in cruxes if isinstance(c, dict)]
    return rows or None


def _cited_evidence_for_pm(
    vault: ObsidianManager, market_id: str, research: dict[str, Any], researched_at: Any
) -> list[dict[str, Any]] | None:
    """Verbatim lines the judges cited (frontmatter ``evidence_bull``/``evidence_bear``), capped.

    When the persisted bundle still holds a matching line, its E id, age/quality label and URL/date are added.
    """
    from orchestrator import evidence  # noqa: PLC0415

    sides = (("yes", research.get("evidence_bull")), ("no", research.get("evidence_bear")))
    if not any(isinstance(v, list) and v for _, v in sides):
        return None
    recs: dict[str, dict[str, Any]] = {}
    try:
        bundle = vault.read_research_bundle(market_id) or []
        today = str(researched_at or "")[:10]
        if bundle and today:
            for r in evidence.label_lines(bundle, today):
                recs.setdefault(r["text"].strip().lstrip("- ").strip(), r)
    except Exception:  # noqa: BLE001 - advisory context only
        recs = {}
    out: list[dict[str, Any]] = []
    total = 0
    for direction, lines in sides:
        for raw in lines if isinstance(lines, list) else []:
            if not isinstance(raw, str):
                continue
            unverified = raw.endswith("(unverified)")
            text = raw[: -len("(unverified)")].strip() if unverified else raw.strip()
            rec = recs.get(text)
            parts = [x.strip() for x in text.split(" | ")]
            item = {
                "cited_for": direction,
                "id": rec["id"] if rec else None,
                "label": None if not rec else (
                    "undated" if rec["age_days"] is None else f"{rec['age_days']}d"
                ) + (" STALE" if rec["stale"] else "") + f" | {rec['quality']}",
                "fact": parts[0],
                "url": rec["url"] if rec and rec.get("url") else (parts[1] if len(parts) > 2 else None),
                "date": parts[-1] if len(parts) > 2 else None,
                "unverified": unverified,
            }
            size = len(str(item))
            if len(out) >= CITED_EVIDENCE_MAX_LINES or total + size > CITED_EVIDENCE_MAX_CHARS:
                return out or None
            out.append(item)
            total += size
    return out or None


def _post_mortem_payload(
    vault: ObsidianManager, market_id: str, resolution: dict[str, Any]
) -> dict[str, Any]:
    """Structured Post-Mortem Analyst input: the decision, the outcome and the realised P&L.

    P&L reuses ``stats.bet_pnl`` (pre-tax per bet in annual_offset mode; the after-tax total is portfolio-level in stats.md) so the analyst, the ledger and
    ``stats.md`` agree. ``None`` fields mean "not available" (e.g. a below-edge market has no bet).
    """
    trade = vault.read_trade_log_dict(market_id) or {}
    research = vault.read_market_record(market_id, "post_mortem") or {}
    filt = vault.read_filter_log(market_id) or {}
    triage = filt.get("triage") if isinstance(filt.get("triage"), dict) else {}
    outcome_yes = ledger.outcome_yes_from_resolution(resolution)

    def first(*vals: Any) -> Any:
        return next((v for v in vals if v is not None), None)

    size = _as_float(trade.get("allocation_usd"))
    size = size if size and size > 0 else None
    price = _as_float(trade.get("price"))
    side = trade.get("side") if size else None
    pnl = None
    if outcome_yes is not None and size is not None and price is not None:
        pnl = stats.bet_pnl(
            {"price": price, "size_usd": size, "shares": trade.get("shares"), "side": side},
            outcome_yes,
        )
    checklist = research.get("resolution_checklist")
    try:
        crux_meta = vault.read_research_bundle_meta(market_id).get("crux_meta")
    except Exception:  # noqa: BLE001 - optional context never fails the post-mortem
        crux_meta = None
    known = (crux_meta or {}).get("known_unknowns") if isinstance(crux_meta, dict) else None
    researched_at = research.get("researched_at") or research.get("as_of")
    return {
        "market_id": market_id,
        "original_research": vault.read_post_mortem(market_id),
        "execution_log": vault.read_trade_log(market_id),
        "resolution_data": resolution,
        "p": _as_float(first(trade.get("p"), research.get("estimated_p"))),
        "p_low": _as_float(first(trade.get("p_low"), research.get("p_low"))),
        "p_high": _as_float(first(trade.get("p_high"), research.get("p_high"))),
        "q_entry": _as_float(first(trade.get("q_entry"), research.get("q_at_research"))),
        "side": side,
        "price": price if size else None,
        "size_usd": size,
        "outcome_yes": outcome_yes,
        "realised_pnl_usd": pnl,
        "category": first(triage.get("category"), filt.get("category")),
        "manipulation_risk": _filter_manipulation_risk(filt),
        "gate_score": _as_float(first(filt.get("score"), filt.get("confidence_multiplier"))),
        "research_confidence": research.get("confidence"),
        "resolution_checklist": checklist if isinstance(checklist, list) else None,
        "crux_table": _crux_table_for_pm(crux_meta),
        "known_unknowns": known if isinstance(known, list) and known else None,
        "cited_evidence": _cited_evidence_for_pm(vault, market_id, research, researched_at),
        "researched_at": researched_at,
        "resolved_at": resolution.get("resolved_at"),
        "end_date": research.get("end_date"),
    }


def _resolve_market(
    vault: ObsidianManager,
    runner: AgentRunner,
    market_id: str,
) -> None:
    """Resolve one market and append post-mortem analysis when possible.

    Idempotent: a post-mortem file left by an earlier failed attempt is reused.
    """
    resolution = scraper.fetch_resolution(market_id)
    if resolution is None:
        return

    if vault.post_mortem_path(market_id).exists():
        log.info("[PHASE 5] post-mortem file exists for %s; re-running analyst", market_id)
    else:
        try:
            vault.move_file(market_id, "active", "post_mortem")
        except FileNotFoundError:
            log.warning("No active research file to move for %s", market_id)
            return
        except FileExistsError:
            log.warning("Post-mortem file already exists for %s; re-running analyst", market_id)

    payload = _post_mortem_payload(vault, market_id, resolution)
    registry = failure_tags.load_registry(vault)
    payload.update(failure_tags.post_mortem_tag_context(registry))
    parsed, reason = run_structured_agent(runner, "post_mortem_analyst", payload)
    if reason:
        _post_mortem_failed(vault, market_id, reason, parsed or payload)
        return

    fclass, tag_slug, tag_def = failure_tags.normalize_failure_class(
        parsed.get("failure_class"), parsed.get("failure_tag_definition"), registry
    )
    to_append = {k: v for k, v in parsed.items() if k != "failure_tag_definition"}
    to_append["failure_class"] = fclass
    try:
        vault.append_post_mortem(market_id, to_append)
    except VaultWriteError as exc:
        _post_mortem_failed(
            vault,
            market_id,
            f"post-mortem append validation failed: {exc.cause}",
            parsed,
        )
        return

    if tag_slug:
        try:
            failure_tags.record_tag(vault, tag_slug, tag_def)
        except Exception:  # noqa: BLE001 - registry is advisory; never block the post-mortem
            log.exception("[PHASE 5] failure tag registry write failed for %s", market_id)
    _ledger_safe(
        vault, market_id, ledger.POST_MORTEM_STAGE,
        failure_class=fclass,
        outcome_yes=payload["outcome_yes"],
        pnl_usd=payload["realised_pnl_usd"],
        p=payload["p"], side=payload["side"], price=payload["price"],
        size_usd=payload["size_usd"], category=payload["category"],
        manipulation_risk=payload["manipulation_risk"],
        **_filter_integrity_fields(vault.read_filter_log(market_id)),
        **_ledger_model_fields("post_mortem_analyst"),
    )
    try:
        vault.archive_trade(market_id)
    except FileNotFoundError:
        log.warning("Trade log already archived for %s", market_id)
