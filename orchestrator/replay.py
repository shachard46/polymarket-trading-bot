"""CLI for clearing inactive flags from native vault directories."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from obsidian_utils import ObsidianManager
from orchestrator.state import VALID_REPLAY_DIRS, replay_inactive

log = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Clear status: inactive and error_log from Vault artifacts in place."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Directory filters (repeatable):\n"
            "  --dir filters   01_Filters/\n"
            "  --dir active    02_Active_Research/\n"
            "  --dir trades    03_Trades/\n"
            "\n"
            "Examples:\n"
            "  %(prog)s --all\n"
            "  %(prog)s --market-id 0xabc --dir filters\n"
            "  %(prog)s --all --dry-run\n"
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
        help="Replay every market with status: inactive in scanned directories.",
    )
    parser.add_argument(
        "--dir",
        action="append",
        dest="dirs",
        choices=sorted(VALID_REPLAY_DIRS),
        metavar="KEY",
        help="Vault directory to scan (repeatable). Default: all of filters, active, trades.",
    )
    parser.add_argument(
        "--vault-path",
        default=None,
        help="Override vault base path (default: OPENCLAW_VAULT_PATH or project root).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be cleared without rewriting files.",
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
    dir_keys = tuple(args.dirs) if args.dirs else None

    vault = ObsidianManager(args.vault_path)
    summary = replay_inactive(
        vault,
        market_ids=market_ids,
        dir_keys=dir_keys,
        dry_run=args.dry_run,
    )

    print(json.dumps(summary, indent=2))
    if (
        summary["cleared"] == 0
        and not args.dry_run
        and (args.all or args.market_ids)
    ):
        log.warning("No inactive flags cleared")
    return 0


if __name__ == "__main__":
    sys.exit(main())
