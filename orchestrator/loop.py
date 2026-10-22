"""Top-level scheduling loop."""

from __future__ import annotations

import logging
import time

from obsidian_utils import ObsidianManager
from orchestrator.config import (
    OVERSEER_INTERVAL_SEC,
    PIPELINE_INTERVAL_SEC,
    RUNNER_MODE_LIVE,
    auto_replay,
    runner_mode,
)
from orchestrator.openclaw_cli import require_gateway
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


def run_forever(vault: ObsidianManager | None = None) -> None:
    """Run the orchestrator forever — phase 1-5 every tick, phase 6 on cadence."""
    if runner_mode() == RUNNER_MODE_LIVE:
        require_gateway()

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
        time.sleep(PIPELINE_INTERVAL_SEC)


__all__ = ["run_pipeline_tick", "run_forever"]
