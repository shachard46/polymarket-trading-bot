"""CLI for restoring quarantined markets from the Dead Letter Queue."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from obsidian_utils import ObsidianManager
from orchestrator.dead_letter import VALID_REPLAY_PHASES, replay_from_dlq

log = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Restore quarantined market artifacts from Vault/05_Errors/.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Phase filters (repeatable, AND): only replay DLQ entries whose "
            "quarantined manifest shows the market completed every listed phase. "
            "Phase 1 (ingestion) has no vault artifact and is always implied.\n"
            "  --phase 2   passed quantitative routing (filter log)\n"
            "  --phase 3   reached active research\n"
            "  --phase 4   reached trade log\n"
            "  --phase 5   reached post-mortem\n"
            "\n"
            "Examples:\n"
            "  %(prog)s --all --phase 2\n"
            "  %(prog)s --all --phase 2 --phase 3\n"
            "  %(prog)s --market-id 0xabc --phase2\n"
        ),
    )
    parser.add_argument(
        "--market-id",
        action="append",
        dest="market_ids",
        default=[],
        metavar="ID",
        help="Market ID to replay (repeatable).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Replay every market with a DLQ error log.",
    )
    phase_group = parser.add_argument_group(
        "phase filter",
        "Restrict replay to markets that had completed the given pipeline phases.",
    )
    phase_group.add_argument(
        "--phase",
        type=int,
        action="append",
        dest="phases",
        choices=sorted(VALID_REPLAY_PHASES),
        metavar="N",
        help="Require phase N completed (1–5). Repeat for AND (e.g. --phase 2 --phase 3).",
    )
    for phase in sorted(VALID_REPLAY_PHASES):
        phase_group.add_argument(
            f"--phase{phase}",
            action="append_const",
            const=phase,
            dest="phases",
            help=argparse.SUPPRESS,
        )
    parser.add_argument(
        "--vault-path",
        default=None,
        help="Override vault base path (default: OPENCLAW_VAULT_PATH or project root).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be restored without moving files.",
    )
    return parser


def _normalize_passed_phases(phases: list[int] | None) -> frozenset[int] | None:
    if not phases:
        return None
    return frozenset(phases)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = _build_parser()
    args = parser.parse_args(argv)

    passed_phases = _normalize_passed_phases(args.phases)
    if not args.all and not args.market_ids and not passed_phases:
        parser.error("Specify --all, at least one --market-id, or a --phase filter")

    if not args.all and not args.market_ids:
        market_ids = None
    else:
        market_ids = None if args.all else args.market_ids

    vault = ObsidianManager(args.vault_path)
    summary = replay_from_dlq(
        vault,
        market_ids=market_ids,
        passed_phases=passed_phases,
        dry_run=args.dry_run,
    )

    print(json.dumps(summary, indent=2))
    if (
        summary["restored"] == 0
        and summary["logs_cleared"] == 0
        and summary.get("logs_filtered", 0) == 0
        and not args.dry_run
    ):
        log.warning("No artifacts restored")
    return 0


if __name__ == "__main__":
    sys.exit(main())
