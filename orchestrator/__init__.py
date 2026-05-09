"""OpenClaw orchestrator package — Hub-and-Spoke Polymarket trading pipeline.

The orchestrator is the only component that touches the filesystem (via
:class:`obsidian_utils.ObsidianManager`) and the only component that spawns
agents. Modules are split by responsibility:

- :mod:`orchestrator.config`      — env flags and tunables
- :mod:`orchestrator.parse`       — JSON/YAML/fenced-block parsing
- :mod:`orchestrator.research`    — Deep Researcher frontmatter parsing
- :mod:`orchestrator.dead_letter` — DLQ quarantine + error helpers
- :mod:`orchestrator.scraper`     — polymarket-scraper integration stubs
- :mod:`orchestrator.runner`      — OpenClaw agent execution adapter
- :mod:`orchestrator.phases`      — six pipeline phases
- :mod:`orchestrator.loop`        — top-level scheduling loop
"""

from __future__ import annotations

from orchestrator.loop import run_forever, run_pipeline_tick

__all__ = ["run_forever", "run_pipeline_tick"]
