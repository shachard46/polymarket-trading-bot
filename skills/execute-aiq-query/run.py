"""CLI entrypoint for execute_aiq_query.

Usage:
    python3 run.py '{"query": "..."}'
"""
from __future__ import annotations

import json
import sys


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: run.py '<json>'", file=sys.stderr)
        raise SystemExit(2)

    from pydantic import ValidationError

    from execute_aiq_query import ExecuteAiqQueryInput, execute_aiq_query

    try:
        payload = json.loads(sys.argv[1])
        args = ExecuteAiqQueryInput.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    result = execute_aiq_query(query=args.query)
    print(result.model_dump_json())


if __name__ == "__main__":
    main()
