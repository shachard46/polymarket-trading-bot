"""OpenClaw Orchestrator entry point.

Lifecycle: Orchestrator builds JSON input → spawns agent → agent uses tools →
agent returns JSON / Markdown → Orchestrator parses output → ObsidianManager
validates and writes. Agents never touch the filesystem directly.

All business logic lives in the :mod:`orchestrator` package; this module
exists only to configure logging and start the loop.
"""

from __future__ import annotations

import logging

from orchestrator import run_forever


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_forever()


if __name__ == "__main__":
    main()
