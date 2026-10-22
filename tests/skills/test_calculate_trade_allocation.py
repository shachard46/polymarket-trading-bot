"""Unit tests for calculate_trade_allocation (docs/03_strategy_math.md)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "calculate_trade_allocation",
    _ROOT / "skills/calculate-trade-allocation/calculate_trade_allocation.py",
)
assert _SPEC and _SPEC.loader
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)
calc = _mod.calculate_trade_allocation

_BASE = dict(q_yes_ask=0.5, L=50_000.0, D=30, bankroll=1000.0)


def test_worked_example_yes():
    out = calc(p=0.8, **_BASE)
    assert out.error is None and out.side == "YES"
    assert out.price == 0.5
    assert out.p_adj == pytest.approx(0.59, abs=1e-6)
    assert out.edge == pytest.approx(0.09, abs=1e-6)
    assert out.kelly_fraction == pytest.approx(0.0108333333, abs=1e-6)
    assert out.allocation_usd == pytest.approx(10.8333333, abs=1e-6)
    assert out.shares == pytest.approx(21.6666667, abs=1e-6)
    assert out.binding_cap == "kelly"
    assert out.below_edge_threshold is False
    assert out.edge_per_day == pytest.approx(0.003, abs=1e-6)


def test_worked_example_no():
    out = calc(p=0.2, q_no_ask=0.48, **_BASE)
    assert out.side == "NO" and out.price == 0.48
    assert out.p_adj == pytest.approx(0.41, abs=1e-6)
    assert out.edge == pytest.approx(0.11, abs=1e-6)
    assert out.allocation_usd == pytest.approx(21.3461538, abs=1e-6)
    assert out.shares == pytest.approx(44.4711538, abs=1e-6)


def test_no_side_when_p_below_q_default_no_ask():
    out = calc(p=0.1, **_BASE)
    assert out.side == "NO" and out.price == pytest.approx(0.5)


def test_below_min_edge_is_zero():
    out = calc(p=0.6, **_BASE)  # p_adj 0.53, edge 0.03
    assert out.below_edge_threshold is True and out.allocation_usd == 0.0
    assert out.error is None


def test_tax_makes_kelly_nonpositive():
    out = calc(p=0.7, **_BASE)  # edge 0.06 but f* < 0 after tax
    assert out.allocation_usd == 0.0 and out.below_edge_threshold is False
    assert out.binding_cap == "kelly_nonpositive"


def test_wide_range_shrinks_to_zero():
    out = calc(p=0.9, p_low=0.5, p_high=1.0, **_BASE)
    assert out.p_adj == pytest.approx(0.5)
    assert out.allocation_usd == 0.0 and out.below_edge_threshold is True


def test_narrow_range_partial_shrink():
    out = calc(p=0.8, p_low=0.7, p_high=0.9, **_BASE)  # factor 0.6
    assert out.p_adj == pytest.approx(0.5 + 0.3 * 0.6 * 0.3, abs=1e-9)


def test_lam_override():
    out = calc(p=0.8, lam=1.0, **_BASE)
    assert out.p_adj == pytest.approx(0.8)


def _big(**kw):
    d = dict(p=0.99, q_yes_ask=0.3, L=1e9, D=30, bankroll=1000.0, lam=1.0)
    d.update(kw)
    return calc(**d)


def test_cap_f_max():
    out = _big()
    assert out.allocation_usd == pytest.approx(100.0) and out.binding_cap == "f_max"


def test_cap_liquidity():
    out = _big(L=400.0)
    assert out.allocation_usd == pytest.approx(20.0) and out.binding_cap == "liquidity"


def test_cap_event():
    out = _big(open_exposure_event=120.0)
    assert out.allocation_usd == pytest.approx(30.0) and out.binding_cap == "event"


def test_cap_total():
    out = _big(open_exposure_total=560.0)
    assert out.allocation_usd == pytest.approx(40.0) and out.binding_cap == "total"


def test_exposure_exhausted_zero():
    out = _big(open_exposure_total=700.0)
    assert out.allocation_usd == 0.0


def test_below_min_order_zero():
    out = _big(L=80.0)  # 0.05*80 = 4 < 5
    assert out.allocation_usd == 0.0 and out.shares == 0.0


@pytest.mark.parametrize(
    "override",
    [
        {"p": float("nan")},
        {"q_yes_ask": float("inf")},
        {"p": 1.5},
        {"p": -0.1},
        {"q_yes_ask": 0.0},
        {"q_yes_ask": 1.0},
        {"q_no_ask": 1.0},
        {"L": 0.0},
        {"D": -1},
        {"bankroll": 0.0},
        {"open_exposure_total": -1.0},
        {"open_exposure_event": float("nan")},
        {"p_low": 0.5},
        {"p_low": 0.9, "p_high": 0.8},
        {"p_low": 0.1, "p_high": 0.2},
        {"lam": 2.0},
    ],
)
def test_invalid_inputs_error(override):
    args = dict(p=0.8, **_BASE)
    args.update(override)
    out = calc(**args)
    assert out.error and "invalid input" in out.error
    assert out.allocation_usd == 0.0 and out.below_edge_threshold is None
