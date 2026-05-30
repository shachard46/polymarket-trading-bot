"""Dead Letter Queue — quarantine markets whose pipeline stage failed.

Per :doc:`docs/02_orchestrator_pipeline.md`, on any agent-level error or
parse failure the orchestrator must:

1. Halt progression for the affected ``market_id``.
2. Move every existing artifact for that market to ``Vault/05_Errors/``.
3. Log the exception details alongside the moved artifacts.
4. Continue the loop with the next market.
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
from pathlib import Path
from typing import Any, Callable, Iterator

from obsidian_utils import ObsidianManager, VaultWriteError

log = logging.getLogger(__name__)

# Vault directory keys that may hold artifacts for a single market_id.
# Order is informational; ``ObsidianManager.move_file`` is keyed by ``market_id``.
QUARANTINE_SOURCE_KEYS: tuple[str, ...] = ("active", "filters", "trades", "post_mortem")

# Extension -> default origin when a legacy DLQ log has no manifest.
_LEGACY_EXT_ORIGIN: dict[str, str] = {
    ".json": "trades",
    ".md": "active",
}


def quarantine_market(
    vault: ObsidianManager,
    market_id: str,
    reason: str,
    payload: dict[str, Any] | None,
) -> None:
    """Move every artifact for ``market_id`` into the DLQ and write an error log."""
    quarantined_artifacts: list[dict[str, str]] = []
    for src_key in QUARANTINE_SOURCE_KEYS:
        try:
            dst = vault.move_file(market_id, src_key, "errors")
        except FileNotFoundError:
            continue
        except KeyError:
            log.exception("Unknown vault key %r while quarantining %s", src_key, market_id)
            continue
        quarantined_artifacts.append(
            {"origin_key": src_key, "stored_filename": dst.name}
        )
    vault.write_error_log(
        market_id,
        payload or {},
        reason,
        quarantined_artifacts=quarantined_artifacts,
    )
    log.warning("Market %s quarantined: %s", market_id, reason)


@contextmanager
def market_quarantine(
    vault: ObsidianManager,
    market_id: str,
    phase_name: str,
) -> Iterator[None]:
    """Quarantine the market if any unhandled exception escapes the block."""
    try:
        yield
    except Exception as exc:  # noqa: BLE001 - pipeline must continue per market
        quarantine_market(
            vault,
            market_id,
            f"{phase_name} exception: {exc!r}",
            {"exception": repr(exc)},
        )


def vault_write_or_quarantine(
    vault: ObsidianManager,
    market_id: str,
    write_fn: Callable[[], Any],
    payload: dict[str, Any],
    artifact_label: str,
) -> bool:
    """Run a validated vault write and quarantine on ``VaultWriteError``."""
    try:
        write_fn()
    except VaultWriteError as exc:
        quarantine_market(
            vault,
            market_id,
            f"{artifact_label} validation failed: {exc.cause}",
            payload,
        )
        return False
    return True


def _legacy_artifact_candidates(
    vault: ObsidianManager,
    market_id: str,
    *,
    exclude_log: Path,
) -> list[tuple[str, str]]:
    """Best-effort artifact list for error logs without a manifest."""
    candidates: list[tuple[str, str]] = []
    for path in vault.iter_quarantined_artifact_paths(market_id, exclude=exclude_log):
        origin = _LEGACY_EXT_ORIGIN.get(path.suffix)
        if origin is None:
            continue
        if path.suffix == ".md":
            log.warning(
                "Legacy DLQ restore for %s: assuming origin %r for %s",
                market_id,
                origin,
                path.name,
            )
        candidates.append((path.name, origin))
    return candidates


def _restore_manifest_entry(
    vault: ObsidianManager,
    entry: dict[str, str],
    *,
    dry_run: bool,
) -> str:
    """Restore one manifest entry. Returns ``restored``, ``skipped``, or ``missing``."""
    origin_key = entry.get("origin_key")
    stored_filename = entry.get("stored_filename")
    if not origin_key or not stored_filename:
        log.warning("Skipping malformed manifest entry: %r", entry)
        return "skipped"

    try:
        dst = vault.restore_artifact(
            stored_filename,
            origin_key,
            dry_run=dry_run,
        )
    except FileNotFoundError:
        log.warning("Quarantined artifact missing during restore: %s", stored_filename)
        return "missing"

    if dst is None:
        return "skipped"
    return "restored"


def replay_from_dlq(
    vault: ObsidianManager,
    market_ids: list[str] | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Restore quarantined artifacts and clear processed DLQ error logs.

    Returns a summary dict with ``restored``, ``skipped``, ``missing``, and
    ``logs_cleared`` counts plus per-market detail.
    """
    summary: dict[str, Any] = {
        "restored": 0,
        "skipped": 0,
        "missing": 0,
        "logs_cleared": 0,
        "markets": {},
    }

    if market_ids is None:
        error_logs = vault.iter_dlq_error_logs()
    else:
        error_logs = []
        for market_id in market_ids:
            error_logs.extend(vault.iter_dlq_error_logs(market_id))

    for log_path in error_logs:
        try:
            record = vault.read_error_log(log_path)
        except (OSError, ValueError) as exc:
            log.warning("Skipping unreadable DLQ log %s: %s", log_path, exc)
            summary["skipped"] += 1
            continue

        market_id = str(record.get("market_id") or "")
        if not market_id:
            log.warning("Skipping DLQ log without market_id: %s", log_path)
            summary["skipped"] += 1
            continue

        manifest = record.get("quarantined_artifacts")
        if not isinstance(manifest, list) or not manifest:
            manifest = [
                {"origin_key": origin, "stored_filename": name}
                for name, origin in _legacy_artifact_candidates(
                    vault, market_id, exclude_log=log_path
                )
            ]

        market_detail: dict[str, Any] = {
            "restored": 0,
            "skipped": 0,
            "missing": 0,
            "artifacts": [],
        }

        for entry in manifest:
            if not isinstance(entry, dict):
                market_detail["skipped"] += 1
                summary["skipped"] += 1
                continue
            outcome = _restore_manifest_entry(vault, entry, dry_run=dry_run)
            market_detail[outcome] += 1
            summary[outcome] += 1
            market_detail["artifacts"].append({**entry, "outcome": outcome})

        if not dry_run:
            vault.discard_error_log(log_path)
            summary["logs_cleared"] += 1

        summary["markets"][market_id] = market_detail

    return summary


__all__ = [
    "QUARANTINE_SOURCE_KEYS",
    "quarantine_market",
    "market_quarantine",
    "vault_write_or_quarantine",
    "replay_from_dlq",
]
