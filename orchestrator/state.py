"""In-place pipeline state — flag inactive markets without moving vault files.

Error-handling contract (see also :mod:`orchestrator.phases`):

- ``flag_inactive`` — explicit agent or parse failures
- ``market_quarantine`` — uncaught exceptions inside a per-market block
- ``vault_write_or_flag`` — :class:`~obsidian_utils.VaultWriteError` on validated writes
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import subprocess
from typing import Any, Callable, Iterator

from obsidian_utils import ObsidianManager, VaultWriteError
from config.trading_constants import (
    ERROR_LOG_KEY,
    STATUS_INACTIVE,
    STATUS_KEY,
)
from orchestrator.openclaw_cli import OpenClawCLIError
from orchestrator.research import split_yaml_frontmatter_markdown

try:  # requests is optional; skills that use it surface its errors here
    from requests.exceptions import RequestException as _RequestException
except ImportError:  # pragma: no cover
    _RequestException = None  # type: ignore[assignment,misc]

log = logging.getLogger(__name__)


class InfraUnavailable(RuntimeError):
    """Gateway/network/disk is unavailable — abort the tick (circuit breaker).

    Raised by :func:`market_quarantine` instead of flagging the market inactive,
    so a transient outage never blacklists markets.
    """


_INFRA_ERRORS: tuple[type[BaseException], ...] = (
    OpenClawCLIError,
    OSError,
    subprocess.TimeoutExpired,
    *((_RequestException,) if _RequestException is not None else ()),
)
# Missing/duplicate vault files are logic errors, not infrastructure outages.
_NON_INFRA_OS_ERRORS: tuple[type[BaseException], ...] = (
    FileNotFoundError,
    FileExistsError,
)


def is_infra_error(exc: BaseException) -> bool:
    """True for transient infrastructure failures that must not flag a market."""
    if isinstance(exc, _NON_INFRA_OS_ERRORS):
        return False
    return isinstance(exc, _INFRA_ERRORS)

PHASE_ARTIFACTS: dict[str, list[str]] = {
    "phase2": ["filters"],
    "phase3": ["active", "filters"],
    "phase4": ["trades", "active"],
    "phase5": ["trades", "post_mortem"],
}

REPLAY_DIRS: tuple[str, ...] = ("filters", "active", "trades", "post_mortem")
VALID_REPLAY_DIRS: frozenset[str] = frozenset(REPLAY_DIRS)


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def is_inactive(record: dict[str, Any] | None) -> bool:
    if not record:
        return False
    return str(record.get(STATUS_KEY) or "").strip().lower() == STATUS_INACTIVE


def _active_frontmatter(
    vault: ObsidianManager,
    market_id: str,
) -> dict[str, Any] | None:
    raw = vault.read_active_research(market_id)
    if not raw:
        return None
    try:
        fm, _ = split_yaml_frontmatter_markdown(raw)
        return fm
    except ValueError:
        return None


def is_error_free_active(vault: ObsidianManager, market_id: str) -> bool:
    """True when active research exists and is not flagged inactive."""
    fm = _active_frontmatter(vault, market_id)
    if fm is None:
        return False
    if is_inactive(fm):
        return False
    agent_err = fm.get("error")
    if agent_err is not None and str(agent_err).strip():
        return False
    return True


def _build_error_log(phase: str, reason: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "reason": reason,
        "phase": phase,
        "logged_at": _utc_now_iso(),
        "payload": payload,
    }


def build_inactive_filter_payload(
    payload: dict[str, Any], phase: str, reason: str
) -> dict[str, Any]:
    """Merge agent payload with ``status: inactive`` and ``error_log``."""
    return {
        **payload,
        STATUS_KEY: STATUS_INACTIVE,
        ERROR_LOG_KEY: _build_error_log(phase, reason, payload),
    }


def _flag_payload(payload: dict[str, Any], phase: str, reason: str) -> dict[str, Any]:
    return build_inactive_filter_payload(payload, phase, reason)


def _inactive_keys(flagged: dict[str, Any]) -> dict[str, Any]:
    return {STATUS_KEY: STATUS_INACTIVE, ERROR_LOG_KEY: flagged[ERROR_LOG_KEY]}


def _minimal_inactive_filter(
    market_id: str,
    flagged: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Smallest valid filter record carrying the inactive stamp."""
    return {
        "market_id": market_id,
        "passed": False,
        "trigger": None,
        "confidence_multiplier": 0.0,
        "details": reason,
        "error": reason,
        **_inactive_keys(flagged),
    }


def _patch_dir_inactive(
    vault: ObsidianManager,
    market_id: str,
    dir_key: str,
    flagged: dict[str, Any],
    reason: str = "",
) -> None:
    keys = _inactive_keys(flagged)
    patched: Any = True
    if dir_key == "filters":
        try:
            vault.write_filter_log(market_id, flagged)
        except VaultWriteError:
            patched = vault.patch_frontmatter(market_id, dir_key, keys)
            if patched is None:
                log.error(
                    "Filter log for %s is corrupt; overwriting with a minimal "
                    "inactive record",
                    market_id,
                )
                vault.write_filter_log(
                    market_id, _minimal_inactive_filter(market_id, flagged, reason)
                )
                return
    elif dir_key in ("active", "post_mortem"):
        patched = vault.patch_frontmatter(market_id, dir_key, keys)
    elif dir_key == "trades":
        patched = vault.patch_json(market_id, dir_key, keys)
    if patched is None:
        log.error(
            "Could not stamp %s inactive in %s (file unreadable/corrupt): %s",
            market_id,
            dir_key,
            reason,
        )


def _cascade_phase3_filter_inactive(
    vault: ObsidianManager,
    market_id: str,
    flagged: dict[str, Any],
) -> None:
    """Stamp filter inactive when active is halted so Phase 3 queue skips the market."""
    if vault.market_file(market_id, "filters") is None:
        return
    filt = vault.read_market_record(market_id, "filters")
    if is_inactive(filt):
        return
    vault.patch_frontmatter(market_id, "filters", _inactive_keys(flagged))


def _primary_artifact_record(
    vault: ObsidianManager,
    market_id: str,
    phase_name: str,
) -> dict[str, Any] | None:
    for dir_key in PHASE_ARTIFACTS.get(phase_name, ["filters"]):
        if vault.market_file(market_id, dir_key) is not None:
            return vault.read_market_record(market_id, dir_key)
    return None


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
            _patch_dir_inactive(vault, market_id, dir_key, flagged, reason)
            if phase == "phase3" and dir_key == "active":
                _cascade_phase3_filter_inactive(vault, market_id, flagged)
            log.warning("Market %s flagged inactive (%s): %s", market_id, phase, reason)
            return

    if phase == "phase2":
        try:
            try:
                vault.write_filter_log(market_id, flagged)
            except VaultWriteError:
                vault.write_filter_log(
                    market_id, _minimal_inactive_filter(market_id, flagged, reason)
                )
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
    """Flag the market inactive if a *content* exception escapes the block.

    Infrastructure errors (see :func:`is_infra_error`) are logged and re-raised as
    :class:`InfraUnavailable` without flagging, so the caller aborts the tick.
    """
    try:
        yield
    except InfraUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - pipeline must continue per market
        if is_infra_error(exc):
            log.error(
                "Infrastructure error for market %s (%s); not flagging, aborting tick: %r",
                market_id,
                phase_name,
                exc,
            )
            raise InfraUnavailable(f"{phase_name} {market_id}: {exc!r}") from exc
        record = _primary_artifact_record(vault, market_id, phase_name)
        if is_inactive(record):
            log.warning(
                "Market %s already inactive (%s); preserving existing error_log",
                market_id,
                phase_name,
            )
            return
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


def _filter_confidence(vault: ObsidianManager, market_id: str) -> float:
    record = vault.read_market_record(market_id, "filters")
    if record is None:
        return 0.0
    try:
        return float(record.get("confidence_multiplier") or 0.0)
    except (TypeError, ValueError):
        return 0.0


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
    min_confidence: float | None = None,
) -> dict[str, Any]:
    """Clear ``status: inactive`` and ``error_log`` from native vault directories.

    When *min_confidence* is given and ``"filters"`` is among the scanned dirs,
    only markets whose ``confidence_multiplier`` strictly exceeds the threshold
    are revived.  Pass ``1.0`` to skip truly-failed phase-2 markets.
    """
    keys = dir_keys or REPLAY_DIRS
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

    if min_confidence is not None and "filters" in keys:
        market_ids = [
            mid for mid in market_ids
            if _filter_confidence(vault, mid) > min_confidence
        ]

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
    "InfraUnavailable",
    "is_infra_error",
    "VALID_REPLAY_DIRS",
    "build_inactive_filter_payload",
    "is_inactive",
    "is_error_free_active",
    "flag_inactive",
    "clear_inactive",
    "market_quarantine",
    "vault_write_or_flag",
    "iter_inactive_market_ids",
    "replay_inactive",
]
