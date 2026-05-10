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
from typing import Any, Callable, Iterator

from obsidian_utils import ObsidianManager, VaultWriteError

log = logging.getLogger(__name__)

# Vault directory keys that may hold artifacts for a single market_id.
# Order is informational; ``ObsidianManager.move_file`` is keyed by ``market_id``.
QUARANTINE_SOURCE_KEYS: tuple[str, ...] = ("active", "filters", "trades", "post_mortem")


def quarantine_market(
    vault: ObsidianManager,
    market_id: str,
    reason: str,
    payload: dict[str, Any] | None,
) -> None:
    """Move every artifact for ``market_id`` into the DLQ and write an error log."""
    for src_key in QUARANTINE_SOURCE_KEYS:
        try:
            vault.move_file(market_id, src_key, "errors")
        except FileNotFoundError:
            continue
        except KeyError:
            log.exception("Unknown vault key %r while quarantining %s", src_key, market_id)
            continue
    vault.write_error_log(market_id, payload or {}, reason)
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


__all__ = [
    "QUARANTINE_SOURCE_KEYS",
    "quarantine_market",
    "market_quarantine",
    "vault_write_or_quarantine",
]
