"""Evidence source (A-IQ vs web searcher, A/B), sanitizer, fallback, stats and paper sizing ledger."""

from __future__ import annotations

import pytest

from obsidian_utils import ObsidianManager
from orchestrator import config as orch_config
from orchestrator.phases import p3_evidence
from orchestrator import evidence, ledger, phases, polymarket_api, stats
from orchestrator.runner import spawn_agent
from tests.orchestrator.test_phase3_helpers import STUB_MARKET_DATA


@pytest.fixture()
def vault(tmp_path):
    v = ObsidianManager(vault_base=tmp_path)
    v.cold_start_protocol()
    return v


def _row(market_id):
    return {
        "market_id": market_id,
        "market_title": "T",
        "market_description": "",
        "market_data": dict(STUB_MARKET_DATA),
    }


def test_config_default_ab_and_unknown_raises(monkeypatch):
    monkeypatch.delenv("OPENCLAW_EVIDENCE_SOURCE", raising=False)
    assert orch_config.evidence_source() == "ab"
    monkeypatch.setenv("OPENCLAW_EVIDENCE_SOURCE", "WEB")
    assert orch_config.evidence_source() == "web"
    monkeypatch.setenv("OPENCLAW_EVIDENCE_SOURCE", "bogus")
    with pytest.raises(ValueError):
        orch_config.evidence_source()


def test_ab_arm_is_stable_and_balanced(monkeypatch):
    ids = [f"0x{i:04x}" for i in range(200)]
    arms = [evidence.ab_arm(m) for m in ids]
    assert arms == [evidence.ab_arm(m) for m in ids]
    assert 60 < arms.count("aiq") < 140 and set(arms) == {"aiq", "web"}
    monkeypatch.setenv("OPENCLAW_EVIDENCE_SOURCE", "ab")
    assert [evidence.intended_source(m) for m in ids] == arms
    monkeypatch.setenv("OPENCLAW_EVIDENCE_SOURCE", "aiq")
    assert {evidence.intended_source(m) for m in ids} == {"aiq"}
    monkeypatch.setenv("OPENCLAW_EVIDENCE_SOURCE", "web")
    assert {evidence.intended_source(m) for m in ids} == {"web"}


def test_sanitizer_redacts_market_and_odds_lines():
    text = "\n".join(
        [
            "Senate voted 52-48 on 2026-10-01 (Reuters)",
            "Polymarket traders give it 63%",
            "Kalshi contract trades at 41c",
            "Bookmakers have the odds at 2/1",
            "Metaculus community forecast is 70%",
            "The bill was signed on Oct 3 (AP)",
        ]
    )
    out = evidence.sanitize_text(text).splitlines()
    assert out[0].startswith("Senate") and out[5].startswith("The bill")
    assert all(line == evidence.REDACTED for line in out[1:5])


def test_aiq_arm_failure_falls_back_to_web(vault, monkeypatch):
    monkeypatch.setenv("OPENCLAW_EVIDENCE_SOURCE", "ab")
    monkeypatch.setattr(evidence, "ab_arm", lambda m: "aiq")
    monkeypatch.setattr(
        p3_evidence,
        "fetch_research_bundle",
        lambda qs: [{"query": q, "research_data": "", "error": "unreachable"} for q in qs],
    )
    meta = phases._fetch_and_persist_bundle(vault, "m1", ["a (as of 2026-10-22)"], spawn_agent, "2026-10-22")
    assert meta == {"evidence_source": "web", "evidence_fallback": True}
    assert vault.read_research_bundle_meta("m1") == meta
    assert "stub web finding" in vault.read_research_bundle("m1")[0]["research_data"]


def test_aiq_arm_success_keeps_aiq_and_no_fallback_in_aiq_mode(vault, monkeypatch):
    monkeypatch.setenv("OPENCLAW_EVIDENCE_SOURCE", "ab")
    monkeypatch.setattr(evidence, "ab_arm", lambda m: "aiq")
    monkeypatch.setattr(
        p3_evidence,
        "fetch_research_bundle",
        lambda qs: [{"query": q, "research_data": "real", "error": None} for q in qs],
    )
    meta = phases._fetch_and_persist_bundle(vault, "m2", ["a"], spawn_agent, "2026-10-22")
    assert meta == {"evidence_source": "aiq", "evidence_fallback": False}

    monkeypatch.setenv("OPENCLAW_EVIDENCE_SOURCE", "aiq")
    monkeypatch.setattr(
        p3_evidence,
        "fetch_research_bundle",
        lambda qs: [{"query": q, "research_data": "", "error": "down"} for q in qs],
    )
    meta = phases._fetch_and_persist_bundle(vault, "m3", ["a"], spawn_agent, "2026-10-22")
    assert meta["evidence_source"] == "aiq" and not meta["evidence_fallback"]


def test_web_path_end_to_end_in_stub_mode(vault, monkeypatch):
    monkeypatch.setenv("OPENCLAW_EVIDENCE_SOURCE", "web")
    out = phases._research_market(vault, spawn_agent, _row("0xweb"), "d")
    assert out is not None
    assert (out["evidence_source"], out["evidence_fallback"]) == ("web", False)
    assert vault.read_market_record("0xweb", "active")["evidence_source"] == "web"
    assert (vault._dirs["active"] / "web_cache.json").exists()
    assert not (vault._dirs["active"] / "aiq_cache.json").exists()
    bundle = vault.read_research_bundle("0xweb")
    assert len(bundle) == 4 and all("stub web finding" in b["research_data"] for b in bundle)


def test_web_searcher_stub_error_yields_error_rows(monkeypatch):
    monkeypatch.setenv("OPENCLAW_ORCHESTRATOR_MODE", "stub_error")
    rows = evidence.fetch_web_rows(spawn_agent, "m", ["q1", "q2"], "2026-10-22")
    assert [r["query"] for r in rows] == ["q1", "q2"]
    assert all(r["error"] and not r["research_data"] for r in rows)
    assert not evidence.rows_usable(rows)


def test_ledger_rows_carry_evidence_source_and_sizing(vault, monkeypatch):
    monkeypatch.setattr(
        polymarket_api,
        "executable_quote",
        lambda m, side: {"ask": 0.31 if side == "YES" else 0.72, "depth_usd_within_2c": 1e9, "spread": 0.02, "token_id": side},
    )
    data = {"yes_price": 0.3, "volume": 10_000.0, "liquidity": 20_000.0, "days_to_resolution": 10}
    phases.phase4_execution(
        vault,
        [{"market_id": "e1", "p_value": 0.8, "p_low": 0.7, "p_high": 0.9, "market_data": data,
          "evidence_source": "web", "evidence_fallback": True}],
    )
    row = next(r for r in ledger.iter_rows(vault) if r["market_id"] == "e1")
    assert row["stage"] == "traded"
    assert (row["evidence_source"], row["evidence_fallback"]) == ("web", True)
    assert row["lam"] > 0 and row["min_edge"] > 0


def test_brier_breakdown_by_evidence_source():
    def d(mid, src, p, q):
        return {"stage": "traded", "market_id": mid, "p": p, "q": q, "evidence_source": src}

    def r(mid, y):
        return {"stage": "resolution", "market_id": mid, "outcome_yes": y}

    rows = [d("a", "aiq", 0.8, 0.5), d("b", "web", 0.2, 0.5), d("c", "web", 0.6, 0.5), d("z", None, 0.5, 0.5),
            r("a", 1), r("b", 0), r("c", 0), r("z", 1)]
    out = stats.brier_by_evidence_source(rows)
    assert out["aiq"] == (1, pytest.approx(0.04), pytest.approx(0.04 - 0.25))
    assert out["web"][0] == 2 and out["web"][1] == pytest.approx((0.04 + 0.36) / 2)
    assert out["unknown"][0] == 1
    s = stats.compute_stats(rows)
    assert "| web | 2 |" in stats.render_stats(s)
