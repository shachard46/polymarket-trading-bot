"""Top-level scheduling loop."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from obsidian_utils import ObsidianManager
from orchestrator.config import (
    OVERSEER_INTERVAL_SEC,
    PIPELINE_INTERVAL_SEC,
    RUNNER_MODE_LIVE,
    auto_replay,
    runner_mode,
)
from orchestrator import notify, runner as _runner
from orchestrator.openclaw_cli import require_gateway
from orchestrator.phases.p4_execution import process_inbox
from orchestrator.phases.p6_overseer import process_directive_votes
from orchestrator.phases import (
    phase1_data_ingestion,
    phase2_quantitative_routing,
    phase3_qualitative_pipeline,
    phase4_execution,
    phase5_resolution_and_post_mortem,
    phase6_macro_learning_loop,
)
from orchestrator.stats import write_stats
from orchestrator.state import InfraUnavailable, replay_inactive

log = logging.getLogger(__name__)


def run_pipeline_tick(vault: ObsidianManager) -> None:
    """Run a single phase 1 → 5 sweep over the scraper queue."""
    tick_id = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    notify.set_tick(tick_id)
    set_nonce = getattr(_runner, "set_run_nonce", None)  # added by the direct-runner work; optional
    if callable(set_nonce):
        set_nonce(tick_id)
    target_ids = phase1_data_ingestion(vault)
    target_ids = [
        m
        for m in target_ids
        if not vault.is_market_inactive(m.market_id, dir_key="filters")
    ]
    phase2_quantitative_routing(vault, target_ids)
    researched = phase3_qualitative_pipeline(vault)
    phase4_execution(vault, researched)
    phase5_resolution_and_post_mortem(vault)
    try:
        write_stats(vault)
    except Exception:  # noqa: BLE001 - measurement must never break a tick
        log.exception("write_stats failed")
    try:
        notify.emit_digest_if_due(vault)
    except Exception:  # noqa: BLE001 - the digest must never break a tick
        log.exception("digest failed")


def _sleep_polling_inbox(vault: ObsidianManager, total_sec: float) -> None:
    """Sleep ``total_sec`` in CONCIERGE_POLL_SEC slices, applying votes after each when approval is on."""
    remaining = float(total_sec)
    while remaining > 0:
        step = min(float(notify.poll_sec()), remaining)
        time.sleep(step)
        remaining -= step
        if notify.approval_enabled():
            try:
                process_inbox(vault)
            except Exception:  # noqa: BLE001
                log.exception("process_inbox failed; continuing")
        if notify.approve_directives_enabled():
            try:
                process_directive_votes(vault)
            except Exception:  # noqa: BLE001
                log.exception("process_directive_votes failed; continuing")


def run_forever(vault: ObsidianManager | None = None) -> None:
    """Run the orchestrator forever — phase 1-5 every tick, phase 6 on cadence."""
    if runner_mode() == RUNNER_MODE_LIVE:
        require_gateway()
    elif runner_mode() == "direct":
        try:
            from orchestrator import direct_runner
        except ImportError:
            direct_runner = None
        check = getattr(direct_runner, "require_api_key", None)
        if callable(check):
            check()

    vault = vault or ObsidianManager()
    if vault.cold_start_protocol():
        log.info("Cold start: wrote seed active_directives.md")

    if auto_replay():
        summary = replay_inactive(vault)
        log.info("Auto inactive replay: %s", summary)

    # Phase 6 first runs after a full interval, not on the first tick.
    last_overseer_run = time.time()
    while True:
        try:
            run_pipeline_tick(vault)
        except InfraUnavailable as exc:
            log.error("Pipeline tick aborted (infrastructure unavailable): %s", exc)
        except Exception:  # noqa: BLE001 - one bad tick must never kill the daemon
            log.exception("Pipeline tick failed; continuing")

        now = time.time()
        if now - last_overseer_run >= OVERSEER_INTERVAL_SEC:
            try:
                phase6_macro_learning_loop(vault)
            except Exception:  # noqa: BLE001
                log.exception("Phase 6 failed; will retry next interval")
            last_overseer_run = now

        log.info("Sleeping %s s until next pipeline tick", PIPELINE_INTERVAL_SEC)
        _sleep_polling_inbox(vault, PIPELINE_INTERVAL_SEC)


__all__ = ["run_pipeline_tick", "run_forever"]
