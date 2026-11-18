"""Pure maths over numbers: Brier, log-loss, bootstrap CI, calibration buckets."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Any

_EPS = 1e-6


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def brier(preds: list[float], outcomes: list[int]) -> float:
    return sum((p - y) ** 2 for p, y in zip(preds, outcomes)) / len(preds)


def _brier_or_none(preds: list[float], ys: list[int]) -> float | None:
    return brier(preds, ys) if ys else None


def _brier_group(pairs: list[tuple[float, int]]) -> tuple[int, float | None]:
    """``(n, Brier)`` of ``(prediction, outcome)`` pairs; Brier is ``None`` when empty."""
    return len(pairs), _brier_or_none([p for p, _ in pairs], [y for _, y in pairs])


def log_loss(preds: list[float], outcomes: list[int]) -> float:
    total = 0.0
    for p, y in zip(preds, outcomes):
        p = min(max(p, _EPS), 1 - _EPS)
        total -= math.log(p) if y else math.log(1 - p)
    return total / len(preds)


def bootstrap_diff_ci(
    triples: list[tuple[float, float, int]], n_boot: int = 1000, seed: int = 0
) -> tuple[float, float]:
    """90% bootstrap CI of Brier(p) - Brier(q); negative means the model beats the market."""
    rng = random.Random(seed)
    n = len(triples)
    sq = [((p - y) ** 2 - (q - y) ** 2) for p, q, y in triples]
    diffs = sorted(sum(sq[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    return diffs[int(0.05 * n_boot)], diffs[min(int(0.95 * n_boot), n_boot - 1)]


def calibration_table(triples: list[tuple[float, float, int]]) -> list[dict[str, Any]]:
    buckets: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for p, _q, y in triples:
        buckets[min(int(p * 10), 9)].append((p, y))
    return [
        {
            "bucket": f"{b * 10}-{b * 10 + 10}%",
            "n": len(v),
            "mean_p": sum(p for p, _ in v) / len(v),
            "hit_rate": sum(y for _, y in v) / len(v),
        }
        for b, v in sorted(buckets.items())
    ]
