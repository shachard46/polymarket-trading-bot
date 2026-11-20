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

import config.trading_constants as _tc


@pytest.fixture(autouse=True)
def _live_sizing(monkeypatch):
    """The worked examples are the live (LAMBDA / MIN_EDGE) numbers."""
    monkeypatch.setattr(_tc, "PAPER_TRADE_MODE", False)
    monkeypatch.setattr(_tc, "SIZING_CONFIDENCE_SIGNAL", "width")  # the range-width tests below; others override


_BASE = dict(q_yes_ask=0.5, L=50_000.0, D=30, bankroll=1000.0)


def test_worked_example_yes():
    # annual_offset (default): pre-tax odds b = (1-c)/c = 1
    out = calc(p=0.8, **_BASE)
    assert out.error is None and out.side == "YES"
    assert out.price == 0.5
    assert out.p_adj == pytest.approx(0.59, abs=1e-6)
    assert out.edge == pytest.approx(0.09, abs=1e-6)
    assert out.kelly_fraction == pytest.approx(0.045, abs=1e-6)
    assert out.allocation_usd == pytest.approx(45.0, abs=1e-6)
    assert out.shares == pytest.approx(90.0, abs=1e-6)
    assert out.binding_cap == "kelly"
    assert out.below_edge_threshold is False
    assert out.edge_per_day == pytest.approx(0.003, abs=1e-6)


def test_worked_example_no():
    out = calc(p=0.2, q_no_ask=0.48, **_BASE)
    assert out.side == "NO" and out.price == 0.48
    assert out.p_adj == pytest.approx(0.41, abs=1e-6)
    assert out.edge == pytest.approx(0.11, abs=1e-6)
    assert out.allocation_usd == pytest.approx(52.8846154, abs=1e-6)
    assert out.shares == pytest.approx(110.1762821, abs=1e-6)


def test_no_side_when_p_below_q_default_no_ask():
    out = calc(p=0.1, **_BASE)
    assert out.side == "NO" and out.price == pytest.approx(0.5)


def test_below_min_edge_is_zero():
    out = calc(p=0.6, **_BASE)  # p_adj 0.53, edge 0.03
    assert out.below_edge_threshold is True and out.allocation_usd == 0.0
    assert out.error is None


def test_small_edge_trades_pre_tax():
    # p=0.70, q=0.50: p_adj 0.56, edge 0.06 >= MIN_EDGE
    out = calc(p=0.7, **_BASE)
    assert out.allocation_usd > 0 and out.binding_cap == "kelly"
    assert out.kelly_fraction == pytest.approx(0.25 * 0.12)  # f* = p_s - (1-p_s) at b = 1


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


def test_effective_helpers_follow_paper_mode(monkeypatch):
    monkeypatch.setattr(_tc, "PAPER_TRADE_MODE", True)
    assert (_tc.effective_lambda(), _tc.effective_min_edge()) == (_tc.PAPER_LAMBDA, _tc.PAPER_MIN_EDGE)
    monkeypatch.setattr(_tc, "PAPER_TRADE_MODE", False)
    assert (_tc.effective_lambda(), _tc.effective_min_edge()) == (_tc.LAMBDA, _tc.MIN_EDGE)


def test_paper_trades_where_live_is_below_edge(monkeypatch):
    kw = dict(p=0.42, q_yes_ask=0.29, L=50_000.0, D=30, bankroll=1000.0)
    live = calc(**kw)  # fixture: live
    assert live.below_edge_threshold is True and live.allocation_usd == 0.0
    assert (live.lam, live.min_edge) == (_tc.LAMBDA, _tc.MIN_EDGE)
    monkeypatch.setattr(_tc, "PAPER_TRADE_MODE", True)
    paper = calc(**kw)
    assert paper.below_edge_threshold is False and paper.allocation_usd > 0
    assert paper.side == "YES"
    assert (paper.lam, paper.min_edge) == (_tc.PAPER_LAMBDA, _tc.PAPER_MIN_EDGE)


def test_explicit_min_edge_overrides_default():
    out = calc(p=0.42, q_yes_ask=0.29, L=50_000.0, D=30, bankroll=1000.0, min_edge=0.03)
    assert out.below_edge_threshold is False and out.min_edge == 0.03


# --- confidence signal (conf_v1) ------------------------------------------------------------------------------


def _signal(monkeypatch, name):
    monkeypatch.setattr(_tc, "SIZING_CONFIDENCE_SIGNAL", name)


@pytest.mark.parametrize("level, weight", [("high", 1.0), ("medium", 0.5)])
def test_code_level_weights(monkeypatch, level, weight):
    _signal(monkeypatch, "code_level")
    out = calc(p=0.8, p_low=0.5, p_high=1.0, confidence_level=level, **_BASE)  # range width ignored
    assert out.p_adj == pytest.approx(0.5 + 0.3 * weight * 0.3)
    assert out.confidence_signal == "code_level" and out.confidence_weight == weight


def test_low_level_is_no_trade_with_distinct_cap(monkeypatch):
    _signal(monkeypatch, "code_level")
    out = calc(p=0.95, confidence_level="low", **_BASE)
    assert out.allocation_usd == 0.0 and out.binding_cap == "low_confidence"
    assert out.below_edge_threshold is True and out.error is None and out.side == "YES"


@pytest.mark.parametrize("score, level_weight", [(10, 1.0), (8, 1.0), (7, 0.5), (4, 0.5), (3, 0.0), (1, 0.0)])
def test_judge_score_mapping(monkeypatch, score, level_weight):
    _signal(monkeypatch, "judge_score")
    out = calc(p=0.8, confidence_score=score, confidence_level="low", **_BASE)  # level ignored here
    assert out.confidence_weight == level_weight
    assert (out.binding_cap == "low_confidence") == (level_weight == 0.0)


def test_width_signal_ignores_level(monkeypatch):
    _signal(monkeypatch, "width")
    out = calc(p=0.8, p_low=0.7, p_high=0.9, confidence_level="low", **_BASE)
    assert out.p_adj == pytest.approx(0.5 + 0.3 * 0.6 * 0.3) and out.confidence_signal == "width"


def test_missing_field_falls_back_to_width(monkeypatch):
    _signal(monkeypatch, "code_level")
    out = calc(p=0.8, p_low=0.7, p_high=0.9, **_BASE)  # old row: no confidence_level
    assert out.confidence_signal == "width" and out.confidence_weight is None
    assert out.p_adj == pytest.approx(0.5 + 0.3 * 0.6 * 0.3)
    _signal(monkeypatch, "judge_score")
    assert calc(p=0.8, p_low=0.7, p_high=0.9, confidence_level="high", **_BASE).confidence_signal == "width"


def test_unknown_signal_errors(monkeypatch):
    _signal(monkeypatch, "bogus")
    assert calc(p=0.8, **_BASE).error


def test_invalid_judge_score_rejected(monkeypatch):
    _signal(monkeypatch, "judge_score")
    assert "confidence_score" in (calc(p=0.8, confidence_score=11, **_BASE).error or "")
