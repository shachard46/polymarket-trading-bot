"""Boundary tests: both sides of the min-edge, event-sibling and thin-evidence thresholds."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import config.trading_constants as _tc
from config.trading_constants import EVENT_SIBLING_MAX_P_SUM, RESEARCH_THIN_EVIDENCE_MAX_GAP
from orchestrator.phases import p3_full, p4_execution
from tests.orchestrator.test_locale_and_guards import _sibling

_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "calculate_trade_allocation_boundaries",
    _ROOT / "skills/calculate-trade-allocation/calculate_trade_allocation.py",
)
assert _SPEC and _SPEC.loader
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)
calc = _mod.calculate_trade_allocation


# ---- 1. minimum edge ----------------------------------------------------------------------------------


@pytest.fixture
def _width_sizing(monkeypatch):
    monkeypatch.setattr(_tc, "PAPER_TRADE_MODE", False)
    monkeypatch.setattr(_tc, "SIZING_CONFIDENCE_SIGNAL", "width")


# All values are exact in binary: q = 0.5, lam = 1 (no shrink, no range -> shrink = lam), min_edge = 0.125.
# p = 0.625: p_adj = 0.5 + 1 * (0.625 - 0.5) = 0.625, edge_yes = 0.125 == min_edge (edge < min_edge is False).
#   b = (1 - 0.5) / 0.5 = 1; f* = (0.625 * 1 - 0.375) / 1 = 0.25; f = KELLY_FRAC 0.25 * 0.25 = 0.0625
#   caps at bankroll 1000: kelly 62.5, f_max 0.10 * 1000 = 100, liquidity 0.05 * 50000 = 2500,
#   event 0.15 * 1000 = 150, total 0.60 * 1000 = 600 -> kelly binds, allocation 62.5, shares 62.5 / 0.5 = 125.
# p = 0.625 - 2**-10 = 0.6240234375: edge = 0.1240234375 < 0.125 -> below_edge_threshold, allocation 0.
@pytest.mark.parametrize(
    "p, below, alloc, cap, shares",
    [
        (0.625, False, 62.5, "kelly", 125.0),
        (0.625 - 2**-10, True, 0.0, "min_edge", 0.0),
    ],
)
def test_min_edge_boundary(_width_sizing, p, below, alloc, cap, shares):
    out = calc(p=p, q_yes_ask=0.5, L=50_000.0, D=30, bankroll=1000.0, lam=1.0, min_edge=0.125)
    assert out.error is None
    assert out.side == "YES"
    assert out.below_edge_threshold is below
    assert out.allocation_usd == alloc
    assert out.binding_cap == cap
    assert out.shares == shares
    assert out.edge == p - 0.5


# ---- 2. event sibling guard ---------------------------------------------------------------------------


def test_sibling_threshold_constant():
    assert EVENT_SIBLING_MAX_P_SUM == 1.15


@pytest.mark.parametrize(
    "my_p, neg_risk, blocked",
    [
        (0.15, True, False),    # 0.15 + 0.5 + 0.5 == 1.15 exactly the constant: strict '>' lets it through
        (0.16, True, True),     # 1.16 > 1.15
        (0.16, False, False),   # same sum but not mutually exclusive
    ],
)
def test_sibling_sum_boundary(vault, my_p, neg_risk, blocked):
    _sibling(vault, "s1", "e1", "researched", p=0.5)
    _sibling(vault, "s2", "e1", "researched", p=0.5)
    md = {"event_id": "e1", "neg_risk": neg_risk}
    reason = p4_execution.event_sibling_guard(vault, "me", md, my_p)
    assert (reason is not None) is blocked
    assert reason in (None, "researched p of 3 mutually exclusive outcomes sums to 1.16 > 1.15")


# ---- 3. thin-evidence gap -----------------------------------------------------------------------------

_UNANSWERED = {"cruxes": [{"role": "evidence", "status": "unanswered"}]}
_ANSWERED = {"cruxes": [{"role": "evidence", "status": "answered"}]}


# max_gap 0.25, q 0.5 (binary-exact): the code skips only when |p - q| > max_gap.
@pytest.mark.parametrize(
    "meta, p, expected",
    [
        (_UNANSWERED, 0.75, False),     # gap exactly 0.25: not above, no skip
        (_UNANSWERED, 0.25, False),     # same, below q
        (_UNANSWERED, 0.7578125, True),  # gap 0.2578125 > 0.25
        (_UNANSWERED, 0.2421875, True),  # gap 0.2578125 below q
        (_ANSWERED, 0.7578125, False),  # evidence answered: no skip however large the gap
    ],
)
def test_thin_evidence_gap_boundary(meta, p, expected):
    assert p3_full.thin_evidence_disagreement(meta, p, 0.5, 0.25) is expected


def test_thin_evidence_default_gap_constant():
    assert RESEARCH_THIN_EVIDENCE_MAX_GAP == 0.15
