"""Top-level scheduling loop."""

from __future__ import annotations

import logging
import time

from obsidian_utils import ObsidianManager
from orchestrator.config import (
    OVERSEER_INTERVAL_SEC,
    PIPELINE_INTERVAL_SEC,
    RUNNER_MODE_LIVE,
    auto_replay_dlq,
    runner_mode,
    top_qualitative_markets,
)
from orchestrator.dead_letter import replay_from_dlq
from orchestrator.openclaw_cli import require_gateway
from orchestrator.phases import (
    merge_phase3_inputs,
    phase1_data_ingestion,
    phase2_quantitative_routing,
    phase3_qualitative_pipeline,
    phase4_execution,
    phase5_resolution_and_post_mortem,
    phase6_macro_learning_loop,
)

log = logging.getLogger(__name__)


def run_pipeline_tick(vault: ObsidianManager) -> None:
    """Run a single phase 1 → 5 sweep over the scraper queue."""
    target_ids = phase1_data_ingestion(vault)
    passed, edge_refresh = phase2_quantitative_routing(vault, target_ids)
    merged = merge_phase3_inputs(
        passed, edge_refresh, top_qualitative_markets()
    )
    researched = phase3_qualitative_pipeline(vault, merged)
    phase4_execution(vault, researched)
    phase5_resolution_and_post_mortem(vault)


def run_forever(vault: ObsidianManager | None = None) -> None:
    """Run the orchestrator forever — phase 1-5 every tick, phase 6 on cadence."""
    if runner_mode() == RUNNER_MODE_LIVE:
        require_gateway()

    vault = vault or ObsidianManager()
    if vault.cold_start_protocol():
        log.info("Cold start: wrote seed active_directives.md")

    if auto_replay_dlq():
        summary = replay_from_dlq(vault)
        log.info("Auto DLQ replay: %s", summary)

    last_overseer_run = 0.0
    while True:
        run_pipeline_tick(vault)

        now = time.time()
        if now - last_overseer_run >= OVERSEER_INTERVAL_SEC:
            phase6_macro_learning_loop(vault)
            last_overseer_run = now

        log.info("Sleeping %s s until next pipeline tick", PIPELINE_INTERVAL_SEC)
        time.sleep(PIPELINE_INTERVAL_SEC)


__all__ = ["run_pipeline_tick", "run_forever"]
