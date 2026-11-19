"""Independent-evidence sample: selection, other-source fetch, measure-only fields."""

from __future__ import annotations

import pytest

from orchestrator import evidence, ledger, phases
from tests.orchestrator.helpers import _ids, _rows
from tests.orchestrator.test_phase3_helpers import deep_researcher_complete
from tests.orchestrator.test_research_redesign import World, _seed


@pytest.fixture()
def world(monkeypatch, vault):
    monkeypatch.setenv("PMBOT_EVIDENCE_SOURCE", "aiq")
    return World(monkeypatch, vault)


from config.trading_constants import INDEP_SAMPLE_RATE


def test_is_indep_sample_rate_and_independence():
    ids = [f"0x{i:05x}" for i in range(6000)]
    flags = [evidence.is_indep_sample(m) for m in ids]
    assert flags == [evidence.is_indep_sample(m) for m in ids]
    assert abs(sum(flags) / len(ids) - INDEP_SAMPLE_RATE) < 0.03
    for arm in (evidence.AIQ, evidence.WEB):
        sub = [f for m, f in zip(ids, flags) if evidence.ab_arm(m) == arm]
        assert abs(sum(sub) / len(sub) - INDEP_SAMPLE_RATE) < 0.04
    sh = [f for m, f in zip(ids, flags) if evidence.is_shadow(m)]
    assert abs(sum(sh) / len(sh) - INDEP_SAMPLE_RATE) < 0.06


def _setup(world, vault, selected=True, p=0.6):
    (mid,) = _ids(lambda m: evidence.is_indep_sample(m) is selected and not evidence.is_shadow(m), 1)
    world.market(mid, 0.42)
    _seed(vault, mid)
    web_calls = []
    orig = world.runner

    def runner(role, payload):
        if role == "web_searcher":
            web_calls.append(payload)
            return {"results": [
                {"query": q, "research_data": "- news | https://e.org/a | unknown\n- Polymarket odds 70% | https://e.org/b | unknown\n- fact | https://e.org/c | unknown", "error": None}
                for q in payload["queries"]
            ]}
        if role == "deep_researcher" and payload.get("session_tag") == "indep":
            world.calls.append((role, payload))
            return deep_researcher_complete(payload, estimated_p=0.7)
        return orig(role, payload)

    world.runner = runner
    world.dr_p = p
    return mid, web_calls


def test_indep_sample_fields_on_report_and_web_sanitized(world, vault):
    mid, web_calls = _setup(world, vault)
    out = phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    (res,) = out
    # aiq main source -> web for the independent sample: one web_searcher call per query (gemini backend)
    assert len(web_calls) == 4 and all(len(c["queries"]) == 1 for c in web_calls)
    indep_calls = [p for r, p in world.calls if r == "deep_researcher" and p.get("session_tag") == "indep"]
    assert len(indep_calls) == 1
    data = " ".join(e["research_data"] for e in indep_calls[0]["research_bundle"]) if "research_bundle" in indep_calls[0] else str(indep_calls[0])
    assert "Polymarket odds" not in data and evidence.REDACTED in data
    assert res["indep_source"] == "web"
    assert res["indep_p"] == pytest.approx(0.7)
    assert res["indep_gap"] == pytest.approx(0.1)
    assert res["p_value"] == pytest.approx(0.6)  # median unaffected
    assert len(world.of("deep_researcher", "full")) == 4


def test_indep_fields_on_traded_row(world, vault):
    mid, _ = _setup(world, vault)
    phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    fm = vault.read_market_record(mid, "active")
    assert fm["indep_p"] == pytest.approx(0.7) and fm["indep_source"] == "web"  # frontmatter keeps it
    phases.phase4_execution(vault, [])  # vault scan path
    rows = [r for r in ledger.iter_rows(vault) if r["stage"] in ("traded", "below_edge") and r["market_id"] == mid]
    assert rows, "market did not reach execution"
    assert rows[-1]["indep_p"] == pytest.approx(0.7)
    assert rows[-1]["indep_gap"] == pytest.approx(0.1)
    assert rows[-1]["indep_source"] == "web"


def test_not_selected_has_no_indep(world, vault):
    mid, web_calls = _setup(world, vault, selected=False)
    (res,) = phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    assert not web_calls and "indep_p" not in res
    assert len(world.of("deep_researcher", "full")) == 3


def test_indep_failure_logged_never_flags(world, vault):
    mid, _ = _setup(world, vault)
    orig = world.runner

    def runner(role, payload):
        if role == "web_searcher":
            raise RuntimeError("web down")
        return orig(role, payload)

    world.runner = runner
    (res,) = phases.phase3_qualitative_pipeline(vault, runner=world.runner)
    assert res["indep_error"] and "indep_p" not in res
    assert not vault.is_market_inactive(mid)
    assert vault.read_active_research(mid) is not None


def test_indep_fields_on_research_skip_row(world, vault):
    mid, _ = _setup(world, vault, p=lambda pl: 0.95)  # |p-q| > gap -> probable_misread
    assert phases.phase3_qualitative_pipeline(vault, runner=world.runner) == []
    (skip,) = _rows(vault, "research_skip")
    assert skip["reason"] == "probable_misread" and skip["indep_source"] == "web"
    assert skip["indep_gap"] == pytest.approx(0.7 - 0.95)
