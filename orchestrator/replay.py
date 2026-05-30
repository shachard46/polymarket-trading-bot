"""CLI for restoring quarantined markets from the Dead Letter Queue."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from obsidian_utils import ObsidianManager
from orchestrator.dead_letter import replay_from_dlq

log = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Restore quarantined market artifacts from Vault/05_Errors/.",
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


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.all and not args.market_ids:
        parser.error("Specify --all or at least one --market-id")

    market_ids = None if args.all else args.market_ids
    vault = ObsidianManager(args.vault_path)
    summary = replay_from_dlq(vault, market_ids=market_ids, dry_run=args.dry_run)

    print(json.dumps(summary, indent=2))
    if summary["restored"] == 0 and summary["logs_cleared"] == 0 and not args.dry_run:
        log.warning("No artifacts restored")
    return 0


if __name__ == "__main__":
    sys.exit(main())
