"""CLI entrypoint for calculate_trade_allocation.

Usage:
    python3 run.py '{"p": 0.7, "q_yes_ask": 0.5, "L": 50000, "D": 30, "bankroll": 1000}'
    echo '{"p": 0.7, "q_yes_ask": 0.5, "L": 50000, "D": 30, "bankroll": 1000}' | python3 run.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def main() -> None:
    if len(sys.argv) > 2:
        print("usage: run.py ['<json>']  (JSON may also be piped on stdin)", file=sys.stderr)
        raise SystemExit(2)

    from calculate_trade_allocation import calculate_trade_allocation

    raw = sys.argv[1] if len(sys.argv) == 2 else sys.stdin.read()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise TypeError("input must be a JSON object")
    except (json.JSONDecodeError, TypeError) as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    # Value validation happens inside the skill and is returned as a structured error.
    try:
        result = calculate_trade_allocation(**payload)
    except TypeError as exc:  # missing / unexpected argument names
        print(f"invalid input: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(result.model_dump_json())


if __name__ == "__main__":
    main()
