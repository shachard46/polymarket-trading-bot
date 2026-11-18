"""Append-only decision ledger: ``<vault>/05_Ledger/decisions.jsonl``.

One JSON object per line. Every pipeline decision (reject, skip, trade, ...) and
every market resolution is recorded so calibration / P&L can be measured later
(see :mod:`orchestrator.stats`).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterator

from obsidian_utils import ObsidianManager

log = logging.getLogger(__name__)

LEDGER_FILENAME = "decisions.jsonl"
RESOLUTION_STAGE = "resolution"
POST_MORTEM_STAGE = "post_mortem"
STAGES = frozenset(
    {
        "gate_reject",
        "triage_reject",
        "research_skip",
        "shadow_research",
        "shadow_failed",
        "triage_shadow",
        "signal_coverage",
        "below_edge",
        "traded",
        "arb_opportunity",
        POST_MORTEM_STAGE,
        RESOLUTION_STAGE,
    }
)


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def ledger_path(vault: ObsidianManager):
    return vault._dirs["ledger"] / LEDGER_FILENAME


def _append(vault: ObsidianManager, row: dict[str, Any]) -> dict[str, Any]:
    path = ledger_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, default=str, sort_keys=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return row


def current_directive_version(vault: ObsidianManager) -> int:
    """``version`` from active_directives.md frontmatter as an int (default 0)."""
    from orchestrator.directives import directive_version

    try:
        return directive_version(vault.read_directives())
    except Exception:  # noqa: BLE001
        return 0


def record(vault: ObsidianManager, market_id: str, stage: str, **fields: Any) -> dict[str, Any]:
    """Append one decision row.

    Common fields: reason, q, p, p_low, p_high, side, price, size_usd, signals,
    category, directive_version (auto-stamped), end_date, event_id.
    """
    if stage not in STAGES:
        raise ValueError(f"unknown ledger stage: {stage!r}")
    row: dict[str, Any] = {"ts": _utc_now(), "market_id": market_id, "stage": stage}
    if "directive_version" not in fields:
        fields["directive_version"] = current_directive_version(vault)
    row.update(fields)
    return _append(vault, row)


def record_resolution(
    vault: ObsidianManager,
    market_id: str,
    outcome_yes: int,
    raw_outcome: Any = None,
    q_close: float | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return record(
        vault,
        market_id,
        RESOLUTION_STAGE,
        outcome_yes=int(outcome_yes),
        raw_outcome=raw_outcome,
        q_close=q_close,
        **extra,
    )


def iter_rows(vault: ObsidianManager) -> Iterator[dict[str, Any]]:
    """Yield ledger rows in file order; malformed lines are skipped."""
    path = ledger_path(vault)
    if not path.exists():
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                log.warning("skipping malformed ledger line")
                continue
            if isinstance(row, dict):
                yield row


def latest_decision_by_market(vault: ObsidianManager) -> dict[str, dict[str, Any]]:
    """Most recent non-resolution row per market."""
    out: dict[str, dict[str, Any]] = {}
    for row in iter_rows(vault):
        if row.get("stage") not in (RESOLUTION_STAGE, POST_MORTEM_STAGE) and row.get("market_id"):
            out[row["market_id"]] = row
    return out


def unresolved_market_ids(vault: ObsidianManager) -> list[str]:
    """Markets with a decision row and no resolution row (first-seen order)."""
    decided: dict[str, None] = {}
    resolved: set[str] = set()
    for row in iter_rows(vault):
        mid = row.get("market_id")
        if not mid or row.get("stage") == "signal_coverage":  # per-tick health rows, not markets
            continue
        if row.get("stage") == RESOLUTION_STAGE:
            resolved.add(mid)
        elif row.get("stage") != POST_MORTEM_STAGE:
            decided.setdefault(mid)
    return [m for m in decided if m not in resolved]


def _parse_price_list(value: Any) -> list[float] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, (list, tuple)) or not value:
        return None
    try:
        return [float(x) for x in value]
    except (TypeError, ValueError):
        return None


def _parse_label_list(value: Any) -> list[str] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, (list, tuple)):
        return [str(x).strip().lower() for x in value]
    return None


def _from_prices(prices: list[float], labels: list[str] | None) -> int | None:
    idx = 0
    if labels and "yes" in labels and len(labels) == len(prices):
        idx = labels.index("yes")
    price = prices[idx]
    if price >= 0.99:
        return 1
    if price <= 0.01:
        return 0
    return None


def _from_scalar(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return 1 if value == 1 else 0 if value == 0 else None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("yes", "y", "true", "1", "1.0"):
            return 1
        if text in ("no", "n", "false", "0", "0.0"):
            return 0
    return None


def outcome_yes_from_resolution(resolution: dict[str, Any] | None) -> int | None:
    """Map a ``scraper.fetch_resolution`` dict to YES=1 / NO=0, else ``None``."""
    if not isinstance(resolution, dict):
        return None
    outcome = resolution.get("outcome")
    scalar = _from_scalar(outcome)
    if scalar is not None:
        return scalar
    raw = resolution.get("raw") if isinstance(resolution.get("raw"), dict) else {}
    labels = _parse_label_list(raw.get("outcomes"))
    for source in (outcome, raw.get("outcomePrices"), raw.get("outcome_prices")):
        prices = _parse_price_list(source)
        if prices:
            return _from_prices(prices, labels)
    return _from_scalar(raw.get("outcome"))


__all__ = [
    "STAGES",
    "current_directive_version",
    "iter_rows",
    "latest_decision_by_market",
    "ledger_path",
    "outcome_yes_from_resolution",
    "record",
    "record_resolution",
    "unresolved_market_ids",
]
