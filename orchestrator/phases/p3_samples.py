"""Phase 3 FULL stage: judge-sample aggregation and the shadow-failure plumbing."""

from __future__ import annotations

import statistics
from typing import Any

from obsidian_utils import ObsidianManager
from orchestrator.agent_outputs import DeepResearcherComplete
from orchestrator.state import flag_inactive

_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

# (judge output, parsed markdown[, framing]); framing is "yes" (J1, asked P(YES)) or "no" (J2, asked P(NO)).
Sample = tuple[DeepResearcherComplete, Any] | tuple[DeepResearcherComplete, Any, str]


class ShadowFailure(Exception):
    """A content/agent error inside a shadow FULL run (ledgered as ``shadow_failed``)."""

    def __init__(self, reason: str, payload: Any = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.payload = payload


def _flag(
    vault: ObsidianManager,
    market_id: str,
    phase: str,
    reason: str,
    payload: Any,
    shadow: bool,
) -> None:
    """Flag the market inactive; a shadow run raises :class:`ShadowFailure` instead (never flags)."""
    if shadow:
        raise ShadowFailure(reason, payload)
    flag_inactive(vault, market_id, phase, reason, payload)


def _framing(sample: Sample) -> str:
    return sample[2] if len(sample) > 2 else "yes"  # type: ignore[misc]


def aggregate_samples(samples: list[Sample]) -> dict[str, Any]:
    """Combine the judge samples into one YES estimate (code decides).

    A ``no``-framed sample (J2, asked P(NO)) is flipped: ``p -> 1 - p`` and its range ``[lo, hi] -> [1 - hi, 1 - lo]``.
    ``p`` is the mean of the flipped estimates, the range is the **union** of the ranges (so
    ``p_low <= p <= p_high``), ``samples`` lists the flipped estimates and ``spread`` is their gap (the framing
    gap; 0 with a single judge). ``chosen`` is the ``yes`` sample (J1, whose markdown is written), else the sample
    nearest the mean. ``confidence`` is the most conservative.
    """
    ps: list[float] = []
    lows: list[float] = []
    highs: list[float] = []
    for s in samples:
        c = s[0]
        if _framing(s) == "no":
            ps.append(1.0 - c.estimated_p)
            lows.append(1.0 - c.p_high)
            highs.append(1.0 - c.p_low)
        else:
            ps.append(c.estimated_p)
            lows.append(c.p_low)
            highs.append(c.p_high)
    p = statistics.fmean(ps)
    yes = [i for i, s in enumerate(samples) if _framing(s) == "yes"]
    chosen = yes[0] if yes else min(range(len(ps)), key=lambda i: (abs(ps[i] - p), i))
    confidence = min((s[0].confidence for s in samples), key=lambda c: _CONFIDENCE_RANK[c])
    return {
        "p": p,
        "p_low": max(0.0, min(min(lows), p)),
        "p_high": min(1.0, max(max(highs), p)),
        "samples": ps,
        "spread": max(ps) - min(ps),
        "confidence": confidence,
        "chosen": chosen,
    }
