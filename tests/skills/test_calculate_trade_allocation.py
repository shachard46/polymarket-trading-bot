"""Unit tests for calculate_trade_allocation validation and caps."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from config.trading_constants import BANKROLL_USD, F_MAX, LIQ_FRAC

_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "calculate_trade_allocation",
    _ROOT / "skills/calculate-trade-allocation/calculate_trade_allocation.py",
)
assert _SPEC and _SPEC.loader
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)
calc = _mod.calculate_trade_allocation

_GOOD = dict(p=0.7, q=0.5, D=30, L=50_000.0, V=100_000.0)


@pytest.mark.parametrize(
    "override",
    [
        {"p": float("nan")},
        {"q": float("inf")},
        {"p": 1.5},
        {"p": 0.0},
        {"q": 0.0},
        {"q": 1.0},
        {"L": -5.0},
        {"L": 0.0},
        {"V": -1.0},
        {"D": -1},
    ],
)
def test_invalid_input_returns_structured_error(override):
    out = calc(**{**_GOOD, **override})
    assert out.error and out.error.startswith("invalid input")
    assert out.allocation_usd == 0.0
    assert out.below_edge_threshold is None


def test_p_equal_one_is_allowed():
    assert calc(**{**_GOOD, "p": 1.0}).error is None


def test_below_edge_zero_allocation():
    out = calc(p=0.5, q=0.5, D=30, L=1000.0, V=1000.0)
    assert out.error is None
    assert out.below_edge_threshold is True
    assert out.allocation_usd == 0.0


def test_fmax_cap():
    out = calc(**_GOOD)
    assert out.below_edge_threshold is False
    assert out.allocation_usd == pytest.approx(F_MAX * BANKROLL_USD)


def test_liquidity_cap():
    out = calc(**{**_GOOD, "L": 400.0})
    assert out.allocation_usd == pytest.approx(LIQ_FRAC * 400.0)


def test_remaining_bankroll_cap():
    out = calc(**_GOOD, remaining_bankroll=12.5)
    assert out.allocation_usd == pytest.approx(12.5)
    assert calc(**_GOOD, remaining_bankroll=0.0).allocation_usd == 0.0
