"""Code-scored confidence level (rule v1), judge score fields, aggregation, p3_full wiring, stats table."""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

import config.trading_constants as tc
from orchestrator import confidence, ledger, phases
from orchestrator.agent_outputs import DeepResearcherComplete
from orchestrator.confidence import compute_confidence
from orchestrator.phases.p3_samples import aggregate_samples
from orchestrator.runner import spawn_agent
from orchestrator.stats import compute_stats, render_stats
from tests.orchestrator.test_crux_pipeline import _stub_world


def _cruxes(ev=("answered",), other=()):
    out = [{"crux_id": f"e{i}", "role": "evidence", "status": s} for i, s in enumerate(ev)]
    out += [{"crux_id": f"o{i}", "role": "mechanism", "status": s} for i, s in enumerate(other)]
    return {"cruxes": out}


_GOOD = dict(
    crux_meta=_cruxes(("answered",), ("answered", "unanswered")),
    source_mix={"official": 2, "major": 2, "blog": 4},  # share 0.5
    prior_kind="reference_class", base_rate_n=12, spread=None, audit_dropped=0,
)


def _c(**over):
    return compute_confidence(**{**_GOOD, **over})


def test_high_when_every_rule_holds():
    out = _c()
    assert out["confidence_level"] == "high" and out["confidence_reasons"] == ["all_high_rules_met"]
    assert _c(prior_kind="structural", base_rate_n=None, spread=tc.CONFIDENCE_HIGH_SPREAD_MAX)["confidence_level"] == "high"


@pytest.mark.parametrize(
    "over, reason",
    [
        (dict(crux_meta=_cruxes(("unanswered",))), "evidence_none_answered"),
        (dict(crux_meta={"cruxes": []}), "evidence_none_answered"),
        (dict(crux_meta=None), "evidence_none_answered"),
        (dict(crux_meta=_cruxes(("answered", "contradicted"))), "evidence_contradicted:1"),
        (dict(spread=0.11), "judge_spread:0.11"),
        (dict(source_mix={"official": 1, "blog": 9}), "official_share:0.10"),
        (dict(source_mix={}), "official_share:0.00"),
        (dict(crux_meta=_cruxes(("unanswered",)), evidence_fallback=True), "evidence_fallback"),
    ],
)
def test_low_rules(over, reason):
    out = _c(**over)
    assert out["confidence_level"] == "low" and reason in out["confidence_reasons"]


@pytest.mark.parametrize(
    "over, reason",
    [
        (dict(crux_meta=_cruxes(("answered", "unanswered"))), "evidence_unanswered:1"),
        (dict(crux_meta=_cruxes(("answered",), ("unanswered", "unanswered"))), "unanswered:2"),
        (dict(source_mix={"official": 1, "major": 2, "blog": 7}), "official_share:0.30"),
        (dict(prior_kind="judgment"), "prior:judgment"),
        (dict(prior_kind="reference_class", base_rate_n=tc.CONFIDENCE_HIGH_BASE_RATE_N_MIN - 1), "prior:reference_class"),
        (dict(audit_dropped=3), "audit_dropped:3"),
        (dict(spread=0.08), "judge_spread:0.08"),
    ],
)
def test_medium_when_a_high_rule_fails(over, reason):
    out = _c(**over)
    assert out["confidence_level"] == "medium" and reason in out["confidence_reasons"]


def test_boundaries():
    assert _c(spread=tc.CONFIDENCE_LOW_SPREAD_MIN)["confidence_level"] == "medium"  # not > 0.10
    assert _c(source_mix={"official": 2, "blog": 8})["confidence_level"] == "medium"  # share exactly 0.20
    assert _c(source_mix={"official": 4, "blog": 6}, spread=tc.CONFIDENCE_HIGH_SPREAD_MAX)["confidence_level"] == "high"  # 0.40, 0.05


def test_never_reads_q():
    params = inspect.signature(compute_confidence).parameters
    assert not {"q", "q_yes_ask", "price"} & set(params)
    src = inspect.getsource(confidence)
    assert "q_yes_ask" not in src and "q_at_research" not in src


# --- judge schema + aggregation -------------------------------------------------------------------------------


def _judge(p=0.6, lo=0.5, hi=0.7, **kw):
    return DeepResearcherComplete(
        status="complete", market_id="m1", estimated_p=p, p_low=lo, p_high=hi, confidence="medium",
        resolution_checklist=[], key_assumptions=[], as_of="2026-11-19", markdown="x", **kw,
    )


@pytest.mark.parametrize("score", [1, 5, 10])
def test_score_accepted(score):
    assert _judge(confidence_score=score, confidence_reason="r").confidence_score == score


@pytest.mark.parametrize("score", [0, 11, -1])
def test_score_out_of_range_rejected(score):
    with pytest.raises(ValidationError):
        _judge(confidence_score=score)


def test_missing_score_is_null_not_failure():
    j = _judge()
    assert j.confidence_score is None and j.confidence_reason is None
    agg = aggregate_samples([(j, None, "yes")])
    assert agg["confidence_score"] is None and agg["confidence_reasons"] == []


def test_aggregate_takes_min_score_and_keeps_reasons():
    a = (_judge(confidence_score=7, confidence_reason="old poll"), None, "yes")
    b = (_judge(confidence_score=4, confidence_reason="thin sources"), None, "no")
    agg = aggregate_samples([a, b])
    assert agg["confidence_score"] == 4 and agg["confidence_reasons"] == ["j1: old poll", "j2: thin sources"]
    one = aggregate_samples([a])
    assert one["confidence_score"] == 7 and one["confidence_reasons"] == ["old poll"]


# --- p3_full wiring: ledger row + frontmatter ---------------------------------------------------------------


def test_full_stage_records_the_four_fields(monkeypatch, vault):
    _stub_world(monkeypatch, vault)
    out = phases.phase3_qualitative_pipeline(vault, runner=spawn_agent)
    assert len(out) == 1
    fm = vault.read_market_record("0xstub", "active")
    for src in (fm, out[0]):
        assert src["confidence_level"] in ("high", "medium", "low")
        assert isinstance(src["confidence_reasons"], list) and src["confidence_reasons"]
        assert "judge_confidence_score" in src and "judge_confidence_reason" in src
    state = vault.read_filter_log("0xstub")["research_state"]
    assert state["confidence_level"] == fm["confidence_level"] and "judge_confidence_score" in state
    assert state["pipeline"] == tc.RESEARCH_PIPELINE == "crux_conf_v1"
    # Phase 4 copies the fields to its ledger row (stub evidence has no official sources: width signal sizes it)
    phases.phase4_execution(vault, out)
    row = next(r for r in ledger.iter_rows(vault) if r["stage"] in ("traded", "below_edge"))
    assert row["confidence_level"] == fm["confidence_level"] and row["confidence_reasons"]


# --- stats table --------------------------------------------------------------------------------------------


def _rows(n_per=2):
    rows = []
    specs = [("high", 9, 0.10), ("low", 2, 0.50)]
    for i, (level, score, width) in enumerate(specs):
        for k in range(n_per):
            m = f"m{i}{k}"
            p = 0.8 if level == "high" else 0.6
            rows.append({"market_id": m, "stage": "below_edge", "p": p, "q": 0.5, "p_low": p - width / 2,
                         "p_high": p + width / 2, "confidence_level": level, "judge_confidence_score": score})
            rows.append({"market_id": m, "stage": ledger.RESOLUTION_STAGE, "outcome_yes": 1 if level == "high" else 0})
    rows.append({"market_id": "old", "stage": "below_edge", "p": 0.5, "q": 0.5})  # pre-conf row
    return rows


def test_confidence_signals_table_renders_with_placeholder():
    s = compute_stats(_rows())
    by = {(e["signal"], e["bucket"]): e for e in s["confidence_signals"]}
    assert by[("code level", "high")]["researched"] == 2 and by[("code level", "high")]["resolved"] == 2
    assert by[("code level", "high")]["brier"] == pytest.approx(0.04)
    assert by[("code level", "low")]["brier"] == pytest.approx(0.36)
    assert by[("judge score", "8-10")]["resolved"] == 2 and by[("judge score", "1-3")]["resolved"] == 2
    assert by[("range width", "<0.20")]["researched"] == 2 and by[("range width", ">0.35")]["researched"] == 2
    assert by[("code level", "unknown")]["researched"] == 1
    assert all(e["lam"] is None for e in s["confidence_signals"])  # n < 50
    text = render_stats(s)
    assert "### Confidence signals" in text and "n<50" in text and "| code level | high |" in text


# --- code level reaches sizing end to end (Phase 4) ---------------------------------------------------------


def test_phase4_code_level_drives_sizing_and_ledger(vault, monkeypatch):
    from orchestrator import polymarket_api
    from orchestrator.phases import p4_execution

    monkeypatch.setattr(tc, "SIZING_CONFIDENCE_SIGNAL", "code_level")  # override the conftest width default
    monkeypatch.setattr(
        polymarket_api, "executable_quote",
        lambda mid, side: {"ask": 0.32 if side == "YES" else 0.72, "depth_usd_within_2c": 1e9, "spread": 0.02, "token_id": side},
    )
    seen: list[dict] = []
    real = p4_execution.compute_allocation
    monkeypatch.setattr(p4_execution, "compute_allocation", lambda *a, **k: seen.append(real(*a, **k)) or seen[-1])
    data = {"yes_price": 0.3, "volume": 10_000.0, "liquidity": 20_000.0, "days_to_resolution": 10}

    def run(mid, level):
        row = {"market_id": mid, "p_value": 0.8, "p_low": 0.7, "p_high": 0.9, "market_data": dict(data),
               "confidence_level": level, "confidence_reasons": [f"{level}_rule"], "judge_confidence_score": 6}
        phases.phase4_execution(vault, [row])
        return next(r for r in ledger.iter_rows(vault) if r["market_id"] == mid)

    med = run("cm", "medium")
    assert seen[-1]["confidence_signal"] == "code_level" and seen[-1]["confidence_weight"] == tc.CONFIDENCE_WEIGHTS["medium"]
    assert med["stage"] == "traded" and med["confidence_level"] == "medium"
    assert vault.read_trade_log_dict("cm")["confidence_level"] == "medium"

    low = run("cl", "low")
    assert seen[-1]["binding_cap"] == "low_confidence" and seen[-1]["confidence_weight"] == 0.0
    assert low["stage"] == "below_edge" and low["reason"] == "low_confidence" and low["confidence_level"] == "low"
