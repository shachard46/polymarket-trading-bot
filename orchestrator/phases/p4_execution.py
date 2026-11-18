"""Phase 4 - Execution: deterministic sizing and the in-process trade skill (no agent)."""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any, Callable

from obsidian_utils import ObsidianManager
from config.trading_constants import BANKROLL_USD, LIQ_FRAC
from orchestrator import ledger, polymarket_api, scraper, stats
from orchestrator.config import PAPER_TRADE_MODE
from orchestrator.phases.common import (
    _as_float,
    _carry_manipulation_risk,
    _ledger_market_fields,
    _ledger_safe,
    _trade_log_shows_edge_disqualification,
    log,
)
from orchestrator.scraper import market_data_hydration_error
from orchestrator.state import (
    flag_inactive,
    is_error_free_active,
    market_quarantine,
    vault_write_or_flag,
)

_ROOT = Path(__file__).resolve().parents[2]
# measure-only independent-evidence sample fields: copied from research to the ledger, never used for sizing
_INDEP_KEYS = ("indep_p", "indep_p_low", "indep_p_high", "indep_source", "indep_gap", "indep_error")
_ALLOCATION_SKILL_PATH = (
    _ROOT / "skills/calculate-trade-allocation/calculate_trade_allocation.py"
)
_allocation_fn: Callable[..., Any] | None = None
_TRADE_SKILL_PATH = (
    _ROOT / "skills/execute-polymarket-trade/execute_polymarket_trade.py"
)
_trade_fn: Callable[..., Any] | None = None


def _load_calculate_trade_allocation() -> Callable[..., Any]:
    global _allocation_fn
    if _allocation_fn is not None:
        return _allocation_fn
    spec = importlib.util.spec_from_file_location(
        "calculate_trade_allocation", _ALLOCATION_SKILL_PATH
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load skill from {_ALLOCATION_SKILL_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _allocation_fn = mod.calculate_trade_allocation
    return _allocation_fn


def _load_execute_polymarket_trade() -> Callable[..., Any]:
    global _trade_fn
    if _trade_fn is not None:
        return _trade_fn
    spec = importlib.util.spec_from_file_location(
        "execute_polymarket_trade", _TRADE_SKILL_PATH
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load skill from {_TRADE_SKILL_PATH}")
    mod = importlib.util.module_from_spec(spec)
    # pydantic resolves the skill's postponed annotations through sys.modules.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    _trade_fn = mod.execute_polymarket_trade
    return _trade_fn


def _open_allocated_usd(vault: ObsidianManager, event_id: str | None = None) -> float:
    """Sum ``allocation_usd`` over open (unresolved) trade logs, optionally for one event."""
    total = 0.0
    for path in vault.iter_open_trades():
        record = vault.read_trade_log_dict(path.stem)
        if not record:
            continue
        if event_id is not None and record.get("event_id") != event_id:
            continue
        try:
            total += max(0.0, float(record.get("allocation_usd") or 0.0))
        except (TypeError, ValueError):
            continue
    return total


def compute_allocation(
    vault: ObsidianManager,
    p: float,
    q: float,
    market_data: dict[str, Any],
    p_low: float | None = None,
    p_high: float | None = None,
    quotes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run ``calculate_trade_allocation`` in-process against bankroll + realised P&L.

    ``quotes`` (``{"YES": quote, "NO": quote}`` from ``polymarket_api.executable_quote``) supplies
    the real asks (``q_yes_ask`` / ``q_no_ask``) and caps the order at the chosen side's
    ``depth_usd_within_2c``; without it the mid ``q`` is the YES ask and NO is ``1 - q``.
    """
    try:
        days = int(float(market_data.get("days_to_resolution")))
    except (TypeError, ValueError):
        days = None  # the skill reports an input error
    event_id = market_data.get("event_id")
    bankroll = BANKROLL_USD + stats.realised_pnl(list(ledger.iter_rows(vault)))
    liquidity = market_data.get("liquidity")

    def run(L: Any) -> dict[str, Any]:
        result = _load_calculate_trade_allocation()(
            p=p,
            q_yes_ask=quotes["YES"]["ask"] if quotes else q,
            q_no_ask=quotes["NO"]["ask"] if quotes else None,
            D=days,
            L=L,
            bankroll=bankroll,
            open_exposure_total=_open_allocated_usd(vault),
            open_exposure_event=_open_allocated_usd(vault, event_id) if event_id else 0.0,
            p_low=p_low,
            p_high=p_high,
        )
        return result.model_dump() if hasattr(result, "model_dump") else dict(result)

    out = run(liquidity)
    side = out.get("side")
    if quotes and side in quotes and not out.get("error"):
        depth = _as_float(quotes[side].get("depth_usd_within_2c"))
        # The skill caps an order at LIQ_FRAC * L; shrinking L to depth / LIQ_FRAC turns that
        # cap into "no more than the ask depth within 2c" without touching the skill.
        if depth is not None and out["allocation_usd"] > depth and isinstance(liquidity, (int, float)):
            out = run(min(float(liquidity), depth / LIQ_FRAC))
    return out


def _phase4_quotes(market_id: str) -> dict[str, dict[str, Any]] | None:
    """Real YES and NO asks + depth from the CLOB, or ``None`` (infra error: skip this tick, no flag)."""
    try:
        yes = polymarket_api.executable_quote(market_id, "YES")
        no = polymarket_api.executable_quote(market_id, "NO")
    except polymarket_api.PolymarketAPIError as exc:
        log.warning("[PHASE 4] quote fetch failed for %s: %s", market_id, exc)
        return None
    return {"YES": yes, "NO": no} if yes and no else None


def _phase4_queue(
    vault: ObsidianManager,
    researched_markets: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], bool]]:
    """This tick's researched markets plus a vault scan; ``(row, from_research)`` deduped by id."""
    queue: list[tuple[dict[str, Any], bool]] = []
    seen: set[str] = set()
    for row in researched_markets:
        if row["market_id"] not in seen:
            seen.add(row["market_id"])
            queue.append((row, True))

    for path in vault.iter_dir_files("active", ".md"):
        market_id = path.stem
        if market_id in seen:
            continue
        if (
            not is_error_free_active(vault, market_id)
            or vault.market_file(market_id, "trades") is not None
            or vault.has_execution_intent(market_id)
            or vault.is_market_inactive(market_id, dir_key="filters")
        ):
            continue
        fm = vault.read_market_record(market_id, "active") or {}
        try:
            p_value = float(fm["estimated_p"])
        except (KeyError, TypeError, ValueError):
            continue
        fresh = scraper.fetch_market_row(market_id)
        if fresh is None:
            log.warning("[PHASE 4] skip %s: could not hydrate market row", market_id)
            continue
        seen.add(market_id)
        queue.append(
            (
                {
                    "market_id": market_id,
                    "p_value": p_value,
                    "p_low": _as_float(fm.get("p_low")),
                    "p_high": _as_float(fm.get("p_high")),
                    "evidence_source": fm.get("evidence_source"),
                    "evidence_fallback": fm.get("evidence_fallback"),
                    **{k: fm.get(k) for k in _INDEP_KEYS if fm.get(k) is not None},
                    "market_data": _carry_manipulation_risk(
                        fresh.market_data, vault.read_filter_log(market_id)
                    ),
                },
                False,
            )
        )
    return queue


def _may_execute(vault: ObsidianManager, market_id: str, from_research: bool) -> bool:
    """False when an intent or a (non-refreshable) trade log already exists for the market."""
    if vault.has_execution_intent(market_id):
        log.error(
            "[PHASE 4] refusing %s: execution intent exists from an earlier "
            "attempt (outcome unknown); resolve manually and delete the intent",
            market_id,
        )
        return False
    existing = vault.read_trade_log_dict(market_id)
    if vault.market_file(market_id, "trades") is not None:
        # Only an edge-disqualified trade from a fresh research refresh may re-execute.
        if not (
            from_research
            and existing is not None
            and _trade_log_shows_edge_disqualification(existing)
        ):
            log.info("[PHASE 4] skip %s: trade log already exists", market_id)
            return False
    return True


def _trade_record(
    vault: ObsidianManager,
    market_id: str,
    market_data: dict[str, Any],
    q: float,
    row: dict[str, Any],
    allocation: dict[str, Any],
    size_usd: float,
    below: bool,
    bought: dict[str, Any],
) -> dict[str, Any]:
    return {
        "market_id": market_id,
        "allocation_usd": size_usd,
        "score": allocation.get("edge"),
        "below_edge_threshold": below,
        "executed": False,
        "transaction_hash": None,
        "error": None,
        "p": row["p_value"],
        "p_low": row.get("p_low"),
        "p_high": row.get("p_high"),
        "q_entry": q,
        "side": allocation.get("side"),
        "price": allocation.get("price"),
        "ask": bought.get("ask"),
        "depth_usd": bought.get("depth_usd_within_2c"),
        "shares": allocation.get("shares"),
        "directive_version": ledger.current_directive_version(vault),
        "event_id": market_data.get("event_id"),
    }


def _place_trade(
    vault: ObsidianManager,
    market_id: str,
    allocation: dict[str, Any],
    size_usd: float,
    trade: dict[str, Any],
) -> bool:
    """Write the intent, call the trade skill, update ``trade``. ``False`` when the call failed."""
    vault.write_execution_intent(
        market_id,
        {
            "market_id": market_id,
            "started_at": time.time(),
            "paper_trade_mode": bool(PAPER_TRADE_MODE),
            "allocation": allocation,
        },
    )
    result = _load_execute_polymarket_trade()(
        market_id=market_id,
        outcome=allocation["side"],
        amount=size_usd,
        price=allocation.get("price"),
    )
    res = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    if not res.get("success"):
        reason = f"execute_polymarket_trade failed: {res.get('error')}"
        log.error(
            "[PHASE 4] trade failed for %s (%s); intent left in place, "
            "market will not be re-executed automatically",
            market_id,
            reason,
        )
        flag_inactive(vault, market_id, "phase4", reason, {**trade, "result": res})
        return False
    if res.get("paper"):
        log.info("[PAPER_TRADE] %s %s $%.2f", market_id, allocation["side"], size_usd)
    trade["executed"] = bool(res.get("success")) and not res.get("paper")
    trade["transaction_hash"] = res.get("transaction_hash")
    return True


def _persist_trade(
    vault: ObsidianManager,
    row: dict[str, Any],
    q: float,
    trade: dict[str, Any],
    allocation: dict[str, Any],
    size_usd: float,
    below: bool,
    bought: dict[str, Any],
) -> None:
    """Write the trade log, clear the intent and ledger ``traded`` / ``below_edge``."""
    market_id = row["market_id"]
    market_data = row.get("market_data") or {}
    if vault_write_or_flag(
        vault=vault,
        market_id=market_id,
        write_fn=lambda: vault.write_trade_log(market_id, trade),
        payload=trade,
        artifact_label="trade log",
        phase="phase4",
    ):
        vault.clear_execution_intent(market_id)
        _ledger_safe(
            vault,
            market_id,
            "below_edge" if below else "traded",
            reason="below_edge_threshold" if below else None,
            q=q,
            p=row["p_value"],
            p_low=row.get("p_low"),
            p_high=row.get("p_high"),
            side=allocation.get("side"),
            price=allocation.get("price"),
            ask=bought.get("ask"),
            depth_usd=bought.get("depth_usd_within_2c"),
            shares=allocation.get("shares"),
            size_usd=size_usd,
            evidence_source=row.get("evidence_source"),
            evidence_fallback=row.get("evidence_fallback"),
            **{k: row[k] for k in _INDEP_KEYS if row.get(k) is not None},
            lam=allocation.get("lam"),
            min_edge=allocation.get("min_edge"),
            **_ledger_market_fields(market_data),
        )


def _execute_market(vault: ObsidianManager, row: dict[str, Any], from_research: bool) -> None:
    """Guards -> quotes -> sizing -> (trade skill) -> trade log + ledger for one market."""
    market_id = row["market_id"]
    market_data = row.get("market_data") or {}
    if not _may_execute(vault, market_id, from_research):
        return

    q = scraper.select_q(market_data)
    if q is None:
        log.warning("[PHASE 4] skip %s this tick: no usable q", market_id)
        return
    hydration_err = market_data_hydration_error(market_data)
    if hydration_err:
        log.warning("[PHASE 4] skip %s this tick: %s", market_id, hydration_err)
        return

    quotes = _phase4_quotes(market_id)
    if quotes is None:
        log.warning("[PHASE 4] skip %s this tick: executable quote unavailable", market_id)
        return

    p_low, p_high = row.get("p_low"), row.get("p_high")
    allocation = compute_allocation(
        vault, row["p_value"], q, market_data, p_low, p_high, quotes=quotes
    )
    if allocation.get("error"):
        log.warning(
            "[PHASE 4] skip %s this tick: allocation error: %s",
            market_id,
            allocation["error"],
        )
        return

    size_usd = float(allocation["allocation_usd"])
    below = bool(allocation["below_edge_threshold"]) or not size_usd > 0
    bought = quotes.get(allocation.get("side") or "") or {}
    trade = _trade_record(vault, market_id, market_data, q, row, allocation, size_usd, below, bought)

    if not below and not _place_trade(vault, market_id, allocation, size_usd, trade):
        return
    _persist_trade(vault, row, q, trade, allocation, size_usd, below, bought)


def phase4_execution(
    vault: ObsidianManager,
    researched_markets: list[dict[str, Any]],
) -> None:
    """Deterministic execution: Hub sizing -> ``execute_polymarket_trade`` -> trade log + ledger.

    No agent is involved. ``compute_allocation`` (shrink toward q, quarter-Kelly, YES/NO
    side) sizes the trade; a zero allocation is ledgered ``below_edge``. Otherwise the
    trade skill is called in-process (``paper: true`` means simulated). An intent record
    guards against double execution across ticks and is left in place on failure.
    """
    log.info("[PHASE 4] Execution")

    for row, from_research in _phase4_queue(vault, researched_markets):
        with market_quarantine(vault, row["market_id"], "phase4"):
            _execute_market(vault, row, from_research)
