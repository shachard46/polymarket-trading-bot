"""In-place pipeline state — flag inactive markets without moving vault files."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import logging
from typing import Any, Callable, Iterator

from obsidian_utils import ObsidianManager, VaultWriteError
from config.trading_constants import (
    ERROR_LOG_KEY,
    PENDING_EDGE_REFRESH_KEY,
    STATUS_INACTIVE,
    STATUS_KEY,
)
from orchestrator.research import split_yaml_frontmatter_markdown

log = logging.getLogger(__name__)

PHASE_ARTIFACTS: dict[str, list[str]] = {
    "phase2": ["filters"],
    "phase3": ["active", "filters"],
    "phase4": ["trades", "active"],
    "phase5": ["post_mortem", "trades"],
}

REPLAY_DIR_KEYS: tuple[str, ...] = ("filters", "active", "trades")
VALID_REPLAY_DIRS: frozenset[str] = frozenset(REPLAY_DIR_KEYS)


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def is_inactive(record: dict[str, Any] | None) -> bool:
    if not record:
        return False
    return str(record.get(STATUS_KEY) or "").strip().lower() == STATUS_INACTIVE


def has_pending_edge_refresh(record: dict[str, Any] | None) -> bool:
    if not record:
        return False
    return bool(record.get(PENDING_EDGE_REFRESH_KEY))


def is_error_free_active(vault: ObsidianManager, market_id: str) -> bool:
    """True when active research exists and is not flagged inactive."""
    raw = vault.read_active_research(market_id)
    if not raw:
        return False
    try:
        fm, _ = split_yaml_frontmatter_markdown(raw)
    except ValueError:
        return False
    if is_inactive(fm):
        return False
    agent_err = fm.get("error")
    if agent_err is not None and str(agent_err).strip():
        return False
    return True


def read_edge_refresh_count(vault: ObsidianManager, market_id: str) -> int:
    raw = vault.read_active_research(market_id)
    if not raw:
        return 0
    try:
        fm, _ = split_yaml_frontmatter_markdown(raw)
    except ValueError:
        return 0
    try:
        return int(fm.get("edge_research_refresh_count") or 0)
    except (TypeError, ValueError):
        return 0


def _build_error_log(phase: str, reason: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "reason": reason,
        "phase": phase,
        "logged_at": _utc_now_iso(),
        "payload": payload,
    }


def _flag_payload(payload: dict[str, Any], phase: str, reason: str) -> dict[str, Any]:
    return {
        **payload,
        STATUS_KEY: STATUS_INACTIVE,
        ERROR_LOG_KEY: _build_error_log(phase, reason, payload),
    }


def flag_inactive(
    vault: ObsidianManager,
    market_id: str,
    phase: str,
    reason: str,
    payload: dict[str, Any] | None,
) -> None:
    """Stamp ``status: inactive`` on the most relevant artifact for ``phase``."""
    body = payload or {}
    flagged = _flag_payload(body, phase, reason)
    candidates = PHASE_ARTIFACTS.get(phase, ["filters"])

    for dir_key in candidates:
        if vault.market_file(market_id, dir_key) is not None:
            if dir_key == "filters":
                try:
                    vault.write_filter_log(market_id, flagged)
                except VaultWriteError:
                    vault.patch_frontmatter(
                        market_id,
                        dir_key,
                        {
                            STATUS_KEY: STATUS_INACTIVE,
                            ERROR_LOG_KEY: flagged[ERROR_LOG_KEY],
                        },
                    )
            elif dir_key == "active":
                vault.patch_frontmatter(
                    market_id,
                    dir_key,
                    {
                        STATUS_KEY: STATUS_INACTIVE,
                        ERROR_LOG_KEY: flagged[ERROR_LOG_KEY],
                    },
                )
            elif dir_key == "trades":
                vault.patch_json(
                    market_id,
                    dir_key,
                    {
                        STATUS_KEY: STATUS_INACTIVE,
                        ERROR_LOG_KEY: flagged[ERROR_LOG_KEY],
                    },
                )
            elif dir_key == "post_mortem":
                vault.patch_frontmatter(
                    market_id,
                    dir_key,
                    {
                        STATUS_KEY: STATUS_INACTIVE,
                        ERROR_LOG_KEY: flagged[ERROR_LOG_KEY],
                    },
                )
            log.warning("Market %s flagged inactive (%s): %s", market_id, phase, reason)
            return

    if phase == "phase2":
        try:
            vault.write_filter_log(market_id, flagged)
            log.warning("Market %s flagged inactive (%s): %s", market_id, phase, reason)
            return
        except VaultWriteError as exc:
            log.error("Could not write inactive filter log for %s: %s", market_id, exc)
            return

    log.error(
        "No artifact to flag for market %s in phase %s: %s",
        market_id,
        phase,
        reason,
    )


def clear_inactive(
    vault: ObsidianManager,
    market_id: str,
    dir_key: str,
    *,
    dry_run: bool = False,
) -> str:
    """Strip inactive state keys. Returns ``cleared``, ``skipped``, or ``missing``."""
    record = vault.read_market_record(market_id, dir_key)
    if record is None:
        return "missing"
    if not is_inactive(record):
        return "skipped"
    if dry_run:
        return "cleared"
    path = vault.strip_keys(market_id, dir_key, (STATUS_KEY, ERROR_LOG_KEY))
    return "cleared" if path is not None else "missing"


@contextmanager
def market_quarantine(
    vault: ObsidianManager,
    market_id: str,
    phase_name: str,
) -> Iterator[None]:
    """Flag the market inactive if any unhandled exception escapes the block."""
    try:
        yield
    except Exception as exc:  # noqa: BLE001 - pipeline must continue per market
        flag_inactive(
            vault,
            market_id,
            phase_name,
            f"{phase_name} exception: {exc!r}",
            {"exception": repr(exc)},
        )


def vault_write_or_flag(
    vault: ObsidianManager,
    market_id: str,
    write_fn: Callable[[], Any],
    payload: dict[str, Any],
    artifact_label: str,
    *,
    phase: str = "phase3",
) -> bool:
    """Run a validated vault write and flag inactive on ``VaultWriteError``."""
    try:
        write_fn()
    except VaultWriteError as exc:
        flag_inactive(
            vault,
            market_id,
            phase,
            f"{artifact_label} validation failed: {exc.cause}",
            payload,
        )
        return False
    return True


def iter_inactive_market_ids(
    vault: ObsidianManager,
    dir_key: str,
) -> list[str]:
    """Return market IDs with ``status: inactive`` in ``dir_key``."""
    suffix = ".md" if dir_key != "trades" else ".json"
    ids: list[str] = []
    for path in vault.iter_dir_files(dir_key, suffix):
        record = vault.read_market_record(path.stem, dir_key)
        if is_inactive(record):
            ids.append(path.stem)
    return ids


def replay_inactive(
    vault: ObsidianManager,
    market_ids: list[str] | None = None,
    *,
    dir_keys: tuple[str, ...] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Clear ``status: inactive`` and ``error_log`` from native vault directories."""
    keys = dir_keys or REPLAY_DIR_KEYS
    unknown = set(keys) - VALID_REPLAY_DIRS
    if unknown:
        raise ValueError(f"Invalid replay dir(s): {sorted(unknown)}")

    summary: dict[str, Any] = {
        "cleared": 0,
        "skipped": 0,
        "missing": 0,
        "dry_run": dry_run,
        "dir_keys": list(keys),
        "markets": {},
    }

    if market_ids is None:
        target_ids: set[str] = set()
        for dir_key in keys:
            target_ids.update(iter_inactive_market_ids(vault, dir_key))
        market_ids = sorted(target_ids)

    for market_id in market_ids:
        detail: dict[str, Any] = {"cleared": 0, "skipped": 0, "missing": 0, "dirs": {}}
        for dir_key in keys:
            outcome = clear_inactive(vault, market_id, dir_key, dry_run=dry_run)
            detail[outcome] += 1
            detail["dirs"][dir_key] = outcome
            summary[outcome] += 1
        summary["markets"][market_id] = detail

    return summary


__all__ = [
    "PHASE_ARTIFACTS",
    "REPLAY_DIR_KEYS",
    "VALID_REPLAY_DIRS",
    "is_inactive",
    "has_pending_edge_refresh",
    "is_error_free_active",
    "read_edge_refresh_count",
    "flag_inactive",
    "clear_inactive",
    "market_quarantine",
    "vault_write_or_flag",
    "iter_inactive_market_ids",
    "replay_inactive",
]
