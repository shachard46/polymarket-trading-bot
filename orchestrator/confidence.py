"""Code-scored confidence level (rule v1): a property of the evidence, never of the market price.

Pure function over measurements the pipeline already holds. It never sees q.
Thresholds live in config/trading_constants.py (CONFIDENCE_*).
"""
from __future__ import annotations

from typing import Any

from config.trading_constants import (
    CONFIDENCE_HIGH_AUDIT_DROPPED_MAX,
    CONFIDENCE_HIGH_BASE_RATE_N_MIN,
    CONFIDENCE_HIGH_OFFICIAL_SHARE_MIN,
    CONFIDENCE_HIGH_SPREAD_MAX,
    CONFIDENCE_HIGH_UNANSWERED_MAX,
    CONFIDENCE_LOW_OFFICIAL_SHARE_MAX,
    CONFIDENCE_LOW_SPREAD_MIN,
)

_STRONG_TIERS = ("official", "major")


def official_share(source_mix: dict[str, Any] | None) -> float | None:
    """Share of official + major sources in a source_mix (tier -> count); None when it is empty."""
    mix = source_mix or {}
    total = sum(max(0, int(v or 0)) for v in mix.values())
    if total <= 0:
        return None
    return sum(max(0, int(mix.get(t) or 0)) for t in _STRONG_TIERS) / total


def compute_confidence(
    crux_meta: dict[str, Any] | None,
    source_mix: dict[str, Any] | None,
    prior_kind: str | None,
    base_rate_n: int | None,
    spread: float | None,
    audit_dropped: int | None,
    evidence_fallback: bool = False,
) -> dict[str, Any]:
    """Rule v1 -> ``{"confidence_level": high|medium|low, "confidence_reasons": [...]}``.

    low if any: no answered evidence crux; an evidence crux contradicted; two-judge spread > 0.10;
    official+major share < 0.20; evidence_fallback without an answered evidence crux.
    high only if all: >= 1 answered and 0 unanswered evidence cruxes; <= 1 unanswered cruxes in total;
    official+major share >= 0.40; reference_class prior with base_rate_n >= 10, or structural prior;
    audit_dropped <= 2; one judge (spread None), or spread <= 0.05. Otherwise medium.
    """
    cruxes = (crux_meta or {}).get("cruxes") or []
    ev = [c for c in cruxes if c.get("role") == "evidence"]
    ev_answered = sum(1 for c in ev if c.get("status") == "answered")
    ev_unanswered = sum(1 for c in ev if c.get("status") == "unanswered")
    ev_contradicted = sum(1 for c in ev if c.get("status") == "contradicted")
    unanswered = sum(1 for c in cruxes if c.get("status") == "unanswered")
    share = official_share(source_mix)
    strong = 0.0 if share is None else share
    dropped = int(audit_dropped or 0)
    reasons: list[str] = []

    if ev_answered == 0:
        reasons.append("evidence_none_answered")
    if ev_contradicted:
        reasons.append(f"evidence_contradicted:{ev_contradicted}")
    if spread is not None and spread > CONFIDENCE_LOW_SPREAD_MIN:
        reasons.append(f"judge_spread:{spread:.2f}")
    if strong < CONFIDENCE_LOW_OFFICIAL_SHARE_MAX:
        reasons.append(f"official_share:{strong:.2f}")
    if evidence_fallback and ev_answered == 0:
        reasons.append("evidence_fallback")
    if reasons:
        return {"confidence_level": "low", "confidence_reasons": reasons}

    if ev_unanswered:
        reasons.append(f"evidence_unanswered:{ev_unanswered}")
    if unanswered > CONFIDENCE_HIGH_UNANSWERED_MAX:
        reasons.append(f"unanswered:{unanswered}")
    if strong < CONFIDENCE_HIGH_OFFICIAL_SHARE_MIN:
        reasons.append(f"official_share:{strong:.2f}")
    prior_ok = prior_kind == "structural" or (
        prior_kind == "reference_class" and (base_rate_n or 0) >= CONFIDENCE_HIGH_BASE_RATE_N_MIN
    )
    if not prior_ok:
        reasons.append(f"prior:{prior_kind or 'none'}")
    if dropped > CONFIDENCE_HIGH_AUDIT_DROPPED_MAX:
        reasons.append(f"audit_dropped:{dropped}")
    if spread is not None and spread > CONFIDENCE_HIGH_SPREAD_MAX:
        reasons.append(f"judge_spread:{spread:.2f}")
    if reasons:
        return {"confidence_level": "medium", "confidence_reasons": reasons}
    return {"confidence_level": "high", "confidence_reasons": ["all_high_rules_met"]}
