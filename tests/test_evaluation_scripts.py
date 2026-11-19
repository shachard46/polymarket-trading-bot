"""Phase C: scripts/backtest.py and scripts/review_research.py (mocked HTTP, fake research fn)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


backtest = _load("backtest")
review = _load("review_research")


def _gamma(i: int, yes: bool = True, outcomes=("Yes", "No")) -> dict:
    prices = ["1", "0"] if yes else ["0", "1"]
    return {"conditionId": f"0x{i}", "question": f"Q{i}?", "description": "rules", "endDate": "2026-11-10T12:00:00Z",
            "startDate": "2026-01-01T00:00:00Z", "outcomes": json.dumps(list(outcomes)), "outcomePrices": json.dumps(prices)}


class _Resp:
    status_code = 200

    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


def test_outcome_and_market_filters():
    assert backtest.outcome_yes(_gamma(1, True)) == 1
    assert backtest.outcome_yes(_gamma(1, False)) == 0
    assert backtest.outcome_yes(_gamma(1, outcomes=("Up", "Down"))) is None
    assert backtest.outcome_yes({**_gamma(1), "outcomePrices": '["0.5","0.5"]'}) is None
    m = backtest.market_from_gamma(_gamma(1), 7)
    assert m["cutoff"] == "2026-11-03" and m["outcome_yes"] == 1
    assert backtest.market_from_gamma({**_gamma(1), "startDate": "2026-11-05T00:00:00Z"}, 7) is None


def test_fetch_resolved_markets_mocked(monkeypatch):
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen.update(params)
        return _Resp([_gamma(1), _gamma(2, outcomes=("A", "B")), _gamma(3, False)])

    monkeypatch.setattr(backtest.requests, "get", fake_get)
    out = backtest.fetch_resolved_markets(days_back=30, days_before=7, limit=10)
    assert [m["market_id"] for m in out] == ["0x1", "0x3"]
    assert seen["closed"] == "true" and "end_date_min" in seen


def test_freshness_range_and_metrics():
    assert backtest.freshness_range("2026-11-03", 10) == "2026-10-24to2026-11-03"
    assert backtest.brier([(1.0, 1), (0.0, 1)]) == 0.5
    assert backtest.log_loss([(0.5, 1), (0.5, 0)]) == pytest.approx(0.693147, abs=1e-5)
    assert backtest.log_loss([(0.0, 1)]) > 10  # clipped, finite


def test_run_backtest_forces_cutoff_and_scores_per_tag():
    calls = []

    def good(row, today, fresh):
        calls.append((today, fresh, set(row)))
        return {"p": 0.9}

    def bad(row, today, fresh):
        raise RuntimeError("boom")

    def tagged(row, today, fresh):
        return {"p": 0.2, "pipeline": "other"}

    markets = [backtest.market_from_gamma(_gamma(1), 7), backtest.market_from_gamma(_gamma(2, False), 7)]
    res = backtest.run_backtest(markets, {"a": good, "b": bad, "c": tagged}, lookback_days=30)
    assert calls[0][0] == "2026-11-03" and calls[0][1] == "2026-10-04to2026-11-03"
    assert "outcome_yes" not in calls[0][2]  # research never sees the outcome
    s = backtest.summarize(res)
    assert s["a"]["n"] == 2 and s["a"]["brier"] == pytest.approx((0.01 + 0.81) / 2)
    assert s["b"]["errors"] == 2 and s["b"]["brier"] is None
    assert "other" in s and "c" not in s


def test_main_refuses_without_brave_key(monkeypatch, tmp_path):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    m = [backtest.market_from_gamma(_gamma(1), 7)]
    assert backtest.main(["--results-dir", str(tmp_path)], research_fn=lambda *a: {"p": 0.5}, markets=m) == 2
    assert not list(tmp_path.iterdir())


def test_main_dry_run_no_research(monkeypatch, capsys):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    m = [backtest.market_from_gamma(_gamma(i), 7) for i in range(5)]

    def boom(*a):
        raise AssertionError("must not run")

    assert backtest.main(["--dry-run", "--max-markets", "3"], research_fn=boom, markets=m) == 0
    assert "dry-run: 3 markets" in capsys.readouterr().out


def test_main_writes_report_and_caps_markets(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    n = []
    m = [backtest.market_from_gamma(_gamma(i), 7) for i in range(5)]
    rc = backtest.main(["--max-markets", "2", "--results-dir", str(tmp_path)],
                       research_fn=lambda r, t, f: n.append(1) or {"p": 0.7}, markets=m)
    assert rc == 0 and len(n) == 2
    (run,) = list(tmp_path.iterdir())
    assert "Leakage caveats" in (run / "report.md").read_text()
    assert json.loads((run / "results.json").read_text())["summary"]["crux_v1"]["n"] == 2


def test_allow_leaky_runs_and_flags(monkeypatch, tmp_path):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    m = [backtest.market_from_gamma(_gamma(1), 7)]
    assert backtest.main(["--allow-leaky", "--results-dir", str(tmp_path)], research_fn=lambda *a: {"p": 0.5}, markets=m) == 0
    (run,) = list(tmp_path.iterdir())
    assert "LEAKY RUN" in (run / "report.md").read_text()


def test_brave_search_freshness(monkeypatch):
    from orchestrator import brave_search

    seen = []

    def fake_get(url, headers=None, params=None, timeout=None):
        seen.append(dict(params))
        return _Resp({"web": {"results": []}})

    monkeypatch.setenv("BRAVE_API_KEY", "k")
    monkeypatch.setattr(brave_search.requests, "get", fake_get)
    brave_search.search("a")
    brave_search.search("b", freshness="pw")
    with brave_search.freshness_override("2026-01-01to2026-02-01"):
        brave_search.search("c")
        brave_search.search("d", freshness="pd")
    brave_search.search("e")
    assert [p.get("freshness") for p in seen] == [None, "pw", "2026-01-01to2026-02-01", "pd", None]


def _vault(tmp_path: Path) -> Path:
    v = tmp_path / "Vault"
    (v / "02_Active_Research" / "research_bundles").mkdir(parents=True)
    (v / "05_Ledger").mkdir()
    for i, ts in enumerate(["2026-11-01T00:00:00+00:00", "2026-11-05T00:00:00+00:00", "2026-11-03T00:00:00+00:00"]):
        (v / "02_Active_Research" / f"0x{i}.md").write_text(
            f"---\nmarket_id: 0x{i}\nestimated_p: 0.4\np_low: 0.3\np_high: 0.5\nq_at_research: 0.2\nresearched_at: '{ts}'\n"
            f"resolution_checklist:\n- YES needs X\n---\n\n# Will X {i}?\n\nbody\n")
    (v / "02_Active_Research" / "research_bundles" / "0x1.json").write_text(json.dumps(
        {"queries": [{"query": "qq", "research_data": "- fact | https://a.org/x | 2026-11-01\nNOT_FOUND"}]}))
    (v / "05_Ledger" / "decisions.jsonl").write_text(
        json.dumps({"market_id": "0x1", "stage": "research_skip", "pipeline": "crux_v1", "q": 0.2}) + "\n")
    return v


def test_packets_recent_first_with_lines_and_tag(tmp_path):
    v = _vault(tmp_path)
    pk = review.build_packets(str(v), 2)
    assert [p["market_id"] for p in pk] == ["0x1", "0x2"]
    assert pk[0]["pipeline"] == "crux_v1" and pk[1]["pipeline"] == "legacy"
    assert pk[0]["cited_lines"][0]["url"] == "https://a.org/x"
    assert pk[0]["question"] == "Will X 1?" and pk[0]["bot"]["q"] == 0.2
    assert "market q at research: 0.2" in review.render_packet_md(pk[0])
    assert "market q" not in review.render_packet_md(review.build_packets(str(v), 1, blind=True)[0])


def test_record_appends_and_validates(tmp_path):
    v = _vault(tmp_path)
    p = review.append_reviews(str(v), [{"market_id": "0x1", "pipeline": "crux_v1", "grade": "good", "own_p": 0.3, "bot_p": 0.4, "notes": "a|b"}])
    review.append_reviews(str(v), [{"market_id": "0x2", "grade": "BAD"}])
    text = p.read_text()
    assert text.count("| GOOD |") == 1 and text.count("| BAD |") == 1 and text.startswith("# Research reviews")
    with pytest.raises(ValueError):
        review.append_reviews(str(v), [{"market_id": "0x3", "grade": "MEH"}])


def test_not_a_vault(tmp_path):
    with pytest.raises(SystemExit):
        review.build_packets(str(tmp_path), 5)
