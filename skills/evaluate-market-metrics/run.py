"""CLI entrypoint for evaluate_market_metrics.

Usage:
    python3 run.py '{"market_id": "0x...", "filter_overrides": {...}}'
"""
from __future__ import annotations

import json
import sys

from pydantic import ValidationError

from evaluate_market_metrics import EvaluateMarketMetricsInput, evaluate_market_metrics


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: run.py '<json>'", file=sys.stderr)
        raise SystemExit(2)

    try:
        payload = json.loads(sys.argv[1])
        args = EvaluateMarketMetricsInput.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    result = evaluate_market_metrics(
        market_id=args.market_id,
        filter_overrides=args.filter_overrides,
    )
    print(result.model_dump_json())


if __name__ == "__main__":
    main()
