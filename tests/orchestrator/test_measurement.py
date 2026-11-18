"""Decision ledger, resolution mapping, stats math, stats.md tick, live prompt date."""

from __future__ import annotations

import random
import re

import pytest

from orchestrator import ledger, loop, phases, scraper, stats
from orchestrator.runner import _build_live_prompt


def test_ledger_round_trip_and_unresolved(vault):
    ledger.record(vault, "a", "gate_reject", reason="r", q=0.4)
    ledger.record(vault, "b", "traded", p=0.7, q=0.5, side="YES", price=0.5, size_usd=10)
    ledger.record(vault, "a", "below_edge", q=0.4)
    rows = list(ledger.iter_rows(vault))
    assert [r["stage"] for r in rows] == ["gate_reject", "traded", "below_edge"]
    assert rows[0]["ts"].endswith("+00:00") and rows[0]["directive_version"] == 1  # seed = v1
    assert ledger.latest_decision_by_market(vault)["a"]["stage"] == "below_edge"
    assert ledger.unresolved_market_ids(vault) == ["a", "b"]
    ledger.record_resolution(vault, "a", 1, "Yes", 0.99)
    assert ledger.unresolved_market_ids(vault) == ["b"]
    with pytest.raises(ValueError):
        ledger.record(vault, "a", "bogus")


@pytest.mark.parametrize(
    "res,expected",
    [
        ({"outcome": "Yes"}, 1),
        ({"outcome": "no"}, 0),
        ({"outcome": None, "raw": {}}, None),
        ({"outcome": None, "raw": {"outcomePrices": '["1","0"]', "outcomes": '["Yes","No"]'}}, 1),
        ({"outcome": None, "raw": {"outcomePrices": ["0", "1"], "outcomes": ["Yes", "No"]}}, 0),
        ({"outcome": None, "raw": {"outcomePrices": ["0", "1"], "outcomes": ["No", "Yes"]}}, 1),
        ({"outcome": ["1", "0"]}, 1),
        ({"outcome": None, "raw": {"outcomePrices": ["0.5", "0.5"]}}, None),
    ],
)
def test_outcome_mapping(res, expected):
    assert ledger.outcome_yes_from_resolution(res) == expected


def test_phase5_resolves_ledger_with_cap(vault, monkeypatch):
    for m in ("a", "b", "c"):
        ledger.record(vault, m, "gate_reject", q=0.3)
    monkeypatch.setenv("OPENCLAW_LEDGER_RESOLVE_LIMIT", "2")
    monkeypatch.setattr(
        scraper, "fetch_resolution", lambda mid: None if mid == "a" else {"outcome": "Yes"}
    )
    phases.phase5_resolution_and_post_mortem(vault)
    assert ledger.unresolved_market_ids(vault) == ["a", "c"]


def test_directive_version(vault):
    assert ledger.current_directive_version(vault) == 1  # seed
    vault.write_file("Vault/00_System/active_directives.md", "---\nversion: 7\n---\n\nx")
    assert ledger.current_directive_version(vault) == 7


def _row(m, stage, **kw):
    return {"market_id": m, "stage": stage, **kw}


def test_brier_and_calibration():
    assert stats.brier([1.0, 0.0], [1, 0]) == 0
    assert stats.brier([0.5, 0.5], [1, 0]) == pytest.approx(0.25)
    rows = [
        _row("a", "traded", p=0.85, q=0.5), _row("a", "resolution", outcome_yes=1),
        _row("b", "traded", p=0.82, q=0.5), _row("b", "resolution", outcome_yes=0),
        _row("c", "traded", p=0.15, q=0.5), _row("c", "resolution", outcome_yes=0),
    ]
    cal = stats.calibration_table(stats.scored_triples(rows))
    assert [(c["bucket"], c["n"]) for c in cal] == [("10-20%", 1), ("80-90%", 2)]
    assert cal[1]["hit_rate"] == 0.5 and cal[1]["mean_p"] == pytest.approx(0.835)


def _pnl_rows(ts_w="2026-03-01T00:00:00+00:00", ts_l="2026-04-01T00:00:00+00:00"):
    w = _row("w", "resolution", outcome_yes=1); w["ts"] = ts_w
    l = _row("l", "resolution", outcome_yes=0); l["ts"] = ts_l
    return [
        _row("w", "traded", p=0.8, q=0.5, side="YES", price=0.5, size_usd=10, category="x", reason="r"), w,
        _row("l", "traded", p=0.8, q=0.5, side="YES", price=0.5, size_usd=10, category="x", reason="r"), l,
        _row("n", "below_edge", q=0.5, side="NO", price=0.5, size_usd=4),
        _row("n", "resolution", outcome_yes=0),
    ]


def test_pnl_annual_offset_and_counterfactual():
    s = stats.compute_stats(_pnl_rows())
    # win +10, loss -10 in the same year: net 0 -> no tax
    assert s["pnl_total"] == pytest.approx(0.0) and s["pnl_tax"] == 0.0
    assert s["pnl_after_tax"] == pytest.approx(0.0)
    assert s["pnl_by_category"]["x"] == (2, pytest.approx(0.0))
    assert s["counterfactual_by_stage"]["below_edge"] == (1, pytest.approx(4.0))
    assert s["counterfactual_after_tax"]["tax"] == pytest.approx(1.0)  # 25% of +4
    assert stats.realised_pnl(_pnl_rows()) == pytest.approx(0.0)


def test_pnl_annual_offset_taxes_net_per_calendar_year():
    rows = _pnl_rows(ts_w="2025-12-31T00:00:00+00:00", ts_l="2026-01-02T00:00:00+00:00")
    s = stats.compute_stats(rows)
    # 2025: +10 -> tax 2.5; 2026: -10 -> no tax, no carry-over
    assert s["pnl_tax_by_year"][2025] == pytest.approx(2.5) and s["pnl_tax_by_year"][2026] == 0.0
    assert s["pnl_tax"] == pytest.approx(2.5)
    assert s["pnl_after_tax"] == pytest.approx(-2.5)
    # per-bet P&L is pre-tax
    assert stats.bet_pnl({"price": 0.5, "size_usd": 10, "side": "YES"}, 1) == pytest.approx(10.0)


def test_false_negative_proxy():
    rows = [
        _row("a", "gate_reject", q=0.1), _row("a", "resolution", outcome_yes=1),
        _row("b", "triage_reject", q=0.9), _row("b", "resolution", outcome_yes=1),
    ]
    assert stats.false_negative_rate(rows) == (2, 0.5)


def test_fit_lambda_recovers_known_value():
    rng = random.Random(1)
    rows = []
    for i in range(4000):
        q, p = rng.uniform(0.1, 0.9), rng.uniform(0.05, 0.95)
        y = int(rng.random() < q + 0.6 * (p - q))
        rows += [_row(f"m{i}", "traded", p=p, q=q), _row(f"m{i}", "resolution", outcome_yes=y)]
    assert stats.fit_lambda(rows) == pytest.approx(0.6, abs=0.1)
    assert stats.fit_lambda(rows[:20]) is None


def test_tick_writes_stats_md(vault, monkeypatch):
    ledger.record(vault, "a", "traded", p=0.7, q=0.5, side="YES", price=0.5, size_usd=5)
    ledger.record_resolution(vault, "a", 1, "Yes")
    monkeypatch.setattr(loop, "phase1_data_ingestion", lambda v: [])
    monkeypatch.setattr(loop, "phase2_quantitative_routing", lambda v, t: None)
    monkeypatch.setattr(loop, "phase3_qualitative_pipeline", lambda v: [])
    monkeypatch.setattr(loop, "phase4_execution", lambda v, r: None)
    monkeypatch.setattr(loop, "phase5_resolution_and_post_mortem", lambda v: None)
    loop.run_pipeline_tick(vault)
    text = (vault._dirs["system"] / "stats.md").read_text()
    assert "Resolved markets: **1**" in text and "Brier" in text


def test_live_prompt_has_date_line():
    prompt = _build_live_prompt("triage", {"output_schema": {}}, {"market_id": "m"})
    assert re.match(r"Today's date: \d{4}-\d{2}-\d{2} \(UTC\)\.\n", prompt)
