"""Six pipeline phases, split into one module per phase (or phase stage).

A phase:

1. Builds the input payload for the appropriate agent.
2. Calls the injected ``runner`` to spawn the agent.
3. Parses the response (JSON/YAML for most agents; markdown for the Deep
   Researcher) using :mod:`orchestrator.parse` and :mod:`orchestrator.research`.
4. Validates and writes the payload via :class:`obsidian_utils.ObsidianManager`.
5. Flags the market inactive in place on any error or parse failure.

Modules: ``common`` (shared helpers), ``agent_call`` (runner->parse->validate plumbing),
``research_state`` (TTL/refresh bookkeeping), ``p1_ingest``, ``p2_routing``, ``p3_quick``,
``p3_full``, ``p3_evidence``, ``p3_queue``, ``p4_execution``, ``p5_resolution``,
``p6_overseer``. This package re-exports every name that callers used to import from the
old ``orchestrator/phases.py``. Tests that monkeypatch must patch the defining submodule.
"""

from __future__ import annotations

from orchestrator.phases.common import Phase3Candidate, Phase3Kind
from orchestrator.phases.p1_ingest import phase1_data_ingestion
from orchestrator.phases.p2_routing import _triage_payload, phase2_quantitative_routing  # noqa: F401
from orchestrator.phases.p3_evidence import _fetch_and_persist_bundle  # noqa: F401
from orchestrator.phases.p3_full import _research_market, aggregate_samples  # noqa: F401
from orchestrator.phases.p3_queue import (
    build_phase3_queue,
    phase3_qualitative_pipeline,
    phase3_research_market,
)
from orchestrator.phases.p3_quick import quick_decision
from orchestrator.phases.p4_execution import (  # noqa: F401
    _load_execute_polymarket_trade,
    _phase4_queue,
    compute_allocation,
    phase4_execution,
)
from orchestrator.phases.p5_resolution import phase5_resolution_and_post_mortem
from orchestrator.phases.p6_overseer import phase6_macro_learning_loop
from orchestrator.phases.research_state import research_ttl_hours

__all__ = [
    "Phase3Candidate",
    "Phase3Kind",
    "phase1_data_ingestion",
    "phase2_quantitative_routing",
    "build_phase3_queue",
    "research_ttl_hours",
    "quick_decision",
    "aggregate_samples",
    "phase3_qualitative_pipeline",
    "phase3_research_market",
    "compute_allocation",
    "phase4_execution",
    "phase5_resolution_and_post_mortem",
    "phase6_macro_learning_loop",
]
