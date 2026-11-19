"""Compound markets ("X and Y both win", "A or B", a sweep): code combines the judge's component probabilities.

The architect flags a conjunction (``and``) or disjunction (``or``) in the title/rules and names its components;
each judge returns one probability per component plus an assumed correlation. Code computes the combined P(YES)
and compares it with the judge's stated P(YES); a gap above ``research_max_sample_spread`` means the judge's headline
number is not backed by its own parts, so the market is skipped (``compound_inconsistent``).

Combination (correlation ``rho`` clamped to [0, COMPOUND_MAX_CORR]; negative correlation is treated as 0):
    AND: (1 - rho) * prod(p_i)         + rho * min(p_i)      (independent .. fully correlated)
    OR : (1 - rho) * (1 - prod(1-p_i)) + rho * max(p_i)
"""

from __future__ import annotations

import math
from typing import Any

from config.trading_constants import COMPOUND_MAX_COMPONENTS, COMPOUND_MAX_CORR

COMPOUND_TYPES = ("and", "or")


def parse_spec(raw: Any) -> dict[str, Any] | None:
    """Validate the architect's ``compound`` object -> ``{"type": "and"|"or", "components": [{"id", "label"}]}`` or None."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type") or "").strip().lower()
    if kind not in COMPOUND_TYPES:
        return None
    comps: list[dict[str, str]] = []
    for c in raw.get("components") or []:
        if isinstance(c, str):
            c = {"id": c, "label": c}
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "").strip()
        if cid and all(cid != x["id"] for x in comps):
            comps.append({"id": cid, "label": str(c.get("label") or cid).strip()})
    if len(comps) < 2:
        return None
    return {"type": kind, "components": comps[:COMPOUND_MAX_COMPONENTS]}


def clamp_corr(rho: float | None) -> float:
    return min(COMPOUND_MAX_CORR, max(0.0, float(rho or 0.0)))


def combine(kind: str, ps: list[float], rho: float | None) -> float:
    """Combined P(YES) of the components (see module docstring)."""
    r = clamp_corr(rho)
    if kind == "and":
        return (1 - r) * math.prod(ps) + r * min(ps)
    if kind == "or":
        return (1 - r) * (1 - math.prod(1 - p for p in ps)) + r * max(ps)
    raise ValueError(f"unknown compound type {kind!r}")


def check(samples: list[Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Compare the combined component probability with the stated one, per judge sample.

    ``samples`` are ``(DeepResearcherComplete, research, framing)``; a ``no``-framed judge's stated P(NO) is flipped, its
    components are always the probability that the component happens. A sample must give every component id.
    Returns ``compound_type``, ``compound_p`` (mean combined), ``compound_stated_p`` (mean stated), ``compound_gap`` (the
    largest per-sample gap) and ``compound_corr`` (mean clamped correlation); the numbers are None when no sample
    carried a full decomposition.
    """
    ids = [c["id"] for c in spec["components"]]
    combined: list[float] = []
    stated: list[float] = []
    corrs: list[float] = []
    for s in samples:
        c = s[0]
        framing = s[2] if len(s) > 2 else "yes"
        by_id = {str(x.id): x.p for x in c.components}
        if any(i not in by_id for i in ids):
            continue
        rho = clamp_corr(c.correlation)
        combined.append(combine(spec["type"], [by_id[i] for i in ids], rho))
        stated.append(1.0 - c.estimated_p if framing == "no" else c.estimated_p)
        corrs.append(rho)
    if not combined:
        return {"compound_type": spec["type"], "compound_p": None, "compound_stated_p": None,
                "compound_gap": None, "compound_corr": None}
    return {
        "compound_type": spec["type"],
        "compound_p": sum(combined) / len(combined),
        "compound_stated_p": sum(stated) / len(stated),
        "compound_gap": max(abs(a - b) for a, b in zip(combined, stated)),
        "compound_corr": sum(corrs) / len(corrs),
    }


__all__ = ["COMPOUND_TYPES", "check", "clamp_corr", "combine", "parse_spec"]
