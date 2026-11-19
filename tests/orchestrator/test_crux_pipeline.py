"""crux_v1 FULL stage: scout -> architect -> crux search -> gap round -> audit -> two judges. No network."""

from __future__ import annotations

import json
from typing import Any

import pytest

from config.trading_constants import GAP_MAX_QUERIES, RESEARCH_MAX_WEB_CALLS
from orchestrator import evidence, ledger, phases, source_quality
from orchestrator.agent_outputs import CruxSpec, DeepResearcherComplete
from orchestrator.phases import p3_evidence, p3_research
from orchestrator.phases.p3_samples import aggregate_samples
from orchestrator.runner import spawn_agent
from tests.orchestrator.test_phase3_helpers import deep_researcher_complete, helper_role_response

TODAY = "2026-11-19"
ROW = {
    "market_id": "m1", "market_title": "Will X win?", "market_description": "rules",
    "market_data": {"end_date": "2026-12-31", "liquidity": 20_000.0, "resolution_source": "Official"},
}


def _line(n: int, domain: str = "reuters.com", date: str = "2026-11-10") -> str:
    return f"- fact {n} | https://{domain}/p{n} | {date}"


class Fx:
    """Scripted runner + fetch recording every payload and query."""

    def __init__(self, monkeypatch, vault):
        self.vault = vault
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.queries: list[str] = []
        self.pages = True  # attach page text to rows (brave-like)
        self.cruxes: list[dict[str, Any]] | None = None
        self.gap: Any = None  # callable(payload) -> dict | None for default stub
        self.audit: Any = None
        self.fail: set[str] = set()
        monkeypatch.setattr(p3_evidence, "fetch_research_bundle", self.fetch)

    def fetch(self, queries):
        rows = []
        for q in queries:
            self.queries.append(q)
            n = len(self.queries)
            row = {"query": q, "research_data": _line(n) + "\n" + _line(n + 100, "blogspot.com"), "error": None}
            if self.pages:
                row["pages"] = [
                    {"url": f"https://reuters.com/p{n}", "text": f"page text for fact {n}"},
                    {"url": f"https://blogspot.com/p{n + 100}", "text": f"other page {n + 100}"},
                ]
            rows.append(row)
        return rows

    def runner(self, role, payload):
        self.calls.append((role, json.loads(json.dumps(payload, default=str))))
        if role in self.fail:
            raise ValueError(f"{role} down")
        if role == "briefer" and self.cruxes is not None:
            return {"market_id": payload["market_id"], "cruxes": self.cruxes, "error": None}
        if role == "gap_analyzer" and self.gap is not None:
            return self.gap(payload)
        if role == "auditor" and self.audit is not None:
            return self.audit(payload)
        if role == "deep_researcher":
            return deep_researcher_complete(payload)
        return helper_role_response(role, payload)

    def of(self, role):
        return [p for r, p in self.calls if r == role]


@pytest.fixture()
def fx(monkeypatch, vault):
    return Fx(monkeypatch, vault)


def _crux(role: str, q: str = "q", n: int = 1) -> dict[str, Any]:
    return {"role": role, "question": f"{role}?", "queries": [f"{q} {role} {i}" for i in range(n)], "moves_p_if": "x"}


# --- cruxes: required roles, counts ---------------------------------------------------------------------


def test_crux_spec_validation():
    with pytest.raises(ValueError):
        CruxSpec.model_validate({"role": "bogus", "question": "q", "queries": ["a"]})
    with pytest.raises(ValueError):
        CruxSpec.model_validate({"role": "skeptic", "question": "q", "queries": []})
    with pytest.raises(ValueError):
        CruxSpec.model_validate({"role": "skeptic", "question": "q", "queries": ["a", "b", "c"]})


def test_missing_required_roles_are_filled_from_templates(fx):
    fx.cruxes = [_crux("evidence"), _crux("evidence", n=2)]  # no mechanism / comparator / horizon / skeptic
    _, meta, _, _ = p3_research.build_evidence(fx.vault, fx.runner, ROW, TODAY, "full")
    roles = [c["role"] for c in meta["cruxes"]]
    assert {"mechanism", "comparator", "horizon", "skeptic"} <= set(roles)
    assert 5 <= len(roles) <= 8
    assert [c["crux_id"] for c in meta["cruxes"]] == [f"c{i}" for i in range(1, len(roles) + 1)]
    assert all("as of 2026-11-19" in q.lower() for q in fx.queries[1:])  # every crux query is dated


def test_crux_count_is_capped_keeping_one_of_each_required_role(fx):
    fx.cruxes = [_crux("evidence", n=1) for _ in range(6)] + [
        _crux(r) for r in ("mechanism", "comparator", "horizon", "skeptic")
    ]
    _, meta, _, _ = p3_research.build_evidence(fx.vault, fx.runner, ROW, TODAY, "full")
    roles = [c["role"] for c in meta["cruxes"]]
    assert len(roles) == 8 and {"mechanism", "comparator", "horizon", "skeptic"} <= set(roles)


def test_lite_depth_has_4_to_5_cruxes_and_no_audit(fx):
    _, meta, _, _ = p3_research.build_evidence(fx.vault, fx.runner, ROW, TODAY, "lite")
    assert 4 <= len(meta["cruxes"]) <= 5
    assert not fx.of("auditor") and "audit_skipped_lite" in meta["flags"]
    assert fx.of("briefer")[0]["depth"] == "lite"


def test_depth_switches_on_potential_max_stake():
    assert p3_research.research_depth({"liquidity": 200.0}) == "lite"  # 5% of 200 = 10 < DEPTH_FULL_MIN_USD
    assert p3_research.research_depth({"liquidity": 20_000.0}) == "full"
    assert p3_research.research_depth({}) == "full"  # no liquidity data: the per-order ceiling applies


# --- no stage sees q ----------------------------------------------------------------------------------------


def test_no_stage_sees_the_market_price(fx, monkeypatch):
    row = {**ROW, "market_data": {**ROW["market_data"], "yes_price": 0.4137, "no_price": 0.5863}}
    (fx.vault).write_filter_log("m1", {"market_id": "m1", "passed": True, "trigger": None,
                                       "confidence_multiplier": 1.0, "details": "ok", "error": None})
    p3_research.build_evidence(fx.vault, fx.runner, row, TODAY, "full")
    samples, _ = p3_research.run_judges(fx.vault, fx.runner, row, "d", TODAY, fx.vault.read_research_bundle_meta("m1")["crux_meta"])
    assert samples
    for role in ("scout", "briefer", "gap_analyzer", "auditor", "deep_researcher"):
        assert fx.of(role), role
    blob = json.dumps(fx.calls)
    assert "0.4137" not in blob and "0.5863" not in blob and "yes_price" not in blob


# --- gap round --------------------------------------------------------------------------------------------


def test_gap_round_is_capped_at_one_round_and_three_queries(fx):
    def gap(payload):
        ids = [c["crux_id"] for c in payload["cruxes"]]
        return {
            "market_id": payload["market_id"],
            "assessments": [{"crux_id": i, "status": "unanswered", "missing": "nothing found"} for i in ids],
            "follow_up_queries": [{"crux_id": i, "query": f"reworded {i}"} for i in ids],  # 5+ proposed
            "error": None,
        }

    fx.gap = gap
    _, meta, _, _ = p3_research.build_evidence(fx.vault, fx.runner, ROW, TODAY, "full")
    assert len(fx.of("gap_analyzer")) == 1  # one round only
    assert meta["gap_queries"] == GAP_MAX_QUERIES == 3
    follow = [q for q in fx.queries if q.startswith("reworded")]
    assert len(follow) == 3 and all("as of 2026-11-19" in q.lower() for q in follow)
    rows = fx.vault.read_research_bundle("m1")
    assert sum(1 for r in rows if r.get("gap")) == 3
    assert meta["known_unknowns"]  # cruxes that stayed unresolved go to the judge


def test_gap_only_follows_up_unanswered_cruxes_not_repeats(fx):
    def gap(payload):
        c0 = payload["cruxes"][0]
        return {
            "market_id": payload["market_id"],
            "assessments": [{"crux_id": c0["crux_id"], "status": "answered", "missing": ""}],
            "follow_up_queries": [{"crux_id": c0["crux_id"], "query": "answered crux follow-up"},
                                  {"crux_id": "c99", "query": "unknown crux"}],
            "error": None,
        }

    fx.gap = gap
    _, meta, _, _ = p3_research.build_evidence(fx.vault, fx.runner, ROW, TODAY, "full")
    assert meta["gap_queries"] == 0


# --- audit ------------------------------------------------------------------------------------------------


def test_audit_drops_unsupported_lines_and_marks_unverified(fx):
    def audit(payload):
        verdicts = [{"id": it["id"], "supported": "blogspot" not in it["url"], "reason": "r"} for it in payload["items"]]
        return {"market_id": payload["market_id"], "verdicts": verdicts, "error": None}

    fx.audit = audit
    rows, meta, _, _ = p3_research.build_evidence(fx.vault, fx.runner, ROW, TODAY, "full")
    items = fx.of("auditor")[0]["items"]
    assert 0 < len(items) <= 10
    assert all(set(it) == {"id", "claim", "url", "page_text"} and it["page_text"] for it in items)
    assert meta["audit_dropped"] >= 1
    stored = fx.vault.read_research_bundle("m1")
    audited_bad = [it["url"] for it in items if "blogspot" in it["url"]]
    assert audited_bad and meta["audit_dropped"] == len(audited_bad)
    stored_text = "\n".join(r["research_data"] for r in stored)
    assert not any(u in stored_text for u in audited_bad)  # failed lines are dropped from the bundle
    assert any(r.get("verified") for r in stored)  # audited, supported lines are verified
    assert all("pages" not in r for r in stored)  # page text never persisted
    assert meta["audit_unverified"] >= 0


def test_audit_cap_is_ten_lines(fx):
    p3_research.build_evidence(fx.vault, fx.runner, ROW, TODAY, "full")
    assert len(fx.of("auditor")[0]["items"]) <= 10


def test_gemini_style_rows_without_page_text_are_unverified_not_dropped(fx):
    fx.pages = False
    rows, meta, _, _ = p3_research.build_evidence(fx.vault, fx.runner, ROW, TODAY, "full")
    assert not fx.of("auditor")  # nothing to check against
    assert meta["audit_dropped"] == 0 and meta["audit_unverified"] > 0
    labelled = evidence.label_bundle(rows, TODAY, quality=True)
    text = "\n".join(r["research_data"] for r in labelled)
    assert "unverified" in text and "reuters.com" in text  # kept, labelled


def test_labels_carry_quality_and_ids_match_label_lines():
    rows = [
        {"query": "q", "crux_id": "c1", "role": "mechanism", "verified": [0],
         "research_data": "- a | https://www.sec.gov/x | 2026-11-18\n- b | https://foo.blogspot.com/y | 2025-01-01\nNOT_FOUND"},
        {"query": "q2", "crux_id": "c2", "role": "skeptic", "research_data": "- c | https://unknown.example/z | unknown"},
    ]
    labelled = evidence.label_bundle(rows, TODAY, quality=True)
    assert labelled[0]["research_data"].splitlines()[0].startswith("[E1 | 1d | official] ")
    assert labelled[0]["research_data"].splitlines()[1].startswith("[E2 | ")
    assert "STALE" in labelled[0]["research_data"] and "blog" in labelled[0]["research_data"].splitlines()[1]
    assert "unverified" in labelled[0]["research_data"].splitlines()[1]
    assert "unverified" not in labelled[0]["research_data"].splitlines()[0]
    assert labelled[1]["research_data"].startswith("[E3 | undated | unknown | unverified] ")
    recs = evidence.label_lines(rows, TODAY)
    assert [r["id"] for r in recs] == ["E1", "E2", "E3"] and recs[0]["quality"] == "official"
    assert recs[1]["stale"] and recs[2]["age_days"] is None and recs[2]["crux_id"] == "c2"
    # the plain (phase A) labels are unchanged
    assert evidence.label_bundle(rows, TODAY)[0]["research_data"].splitlines()[0].startswith("[E1 | 1d] ")


def test_source_quality_map_and_heuristics():
    q = source_quality.quality_for
    assert q("https://www.reuters.com/a") == "major" and q("https://news.reuters.com/a") == "major"
    assert q("https://sec.gov/x") == "official" and q("https://someagency.gov/x") == "official"
    assert q("https://en.wikipedia.org/wiki/X") == "aggregator"
    assert q("https://random.blogspot.com/x") == "blog" and q("https://myblog.example/x") == "blog"
    assert q("https://unlisted.example/x") == "unknown" and q("not a url") == "unknown"


def test_source_quality_unknown_tier_raises(tmp_path):
    bad = tmp_path / "sq.yaml"
    bad.write_text("shiny:\n  - example.com\n")
    with pytest.raises(ValueError):
        source_quality.quality_for("https://example.com/x", bad)


# --- failure fallbacks ------------------------------------------------------------------------------------


@pytest.mark.parametrize("role,flag", [("scout", "scout_failed"), ("briefer", "architect_failed"),
                                       ("gap_analyzer", "gap_failed"), ("auditor", "audit_failed")])
def test_helper_role_failure_degrades_and_is_flagged(fx, role, flag):
    fx.fail.add(role)
    _, meta, _, _ = p3_research.build_evidence(fx.vault, fx.runner, ROW, TODAY, "full")
    assert flag in meta["flags"]
    assert len(meta["cruxes"]) >= 5  # the pipeline still produced evidence
    assert fx.vault.read_research_bundle("m1")


def test_scout_failure_leaves_architect_working_from_rules(fx):
    fx.fail.add("scout")
    p3_research.build_evidence(fx.vault, fx.runner, ROW, TODAY, "full")
    assert fx.of("briefer")[0]["scout"] is None


def test_scout_landscape_reaches_the_architect(fx):
    p3_research.build_evidence(fx.vault, fx.runner, ROW, TODAY, "full")
    scout = fx.of("briefer")[0]["scout"]
    assert scout and "event" in scout and "market_id" not in scout


def test_one_judge_fails_other_is_used_both_fail_is_empty(fx):
    p3_research.build_evidence(fx.vault, fx.runner, ROW, TODAY, "full")
    meta = fx.vault.read_research_bundle_meta("m1")["crux_meta"]

    def j1_down(role, payload):
        if role == "deep_researcher" and payload["session_tag"] == "j1":
            raise ValueError("down")
        return fx.runner(role, payload)

    samples, failed = p3_research.run_judges(fx.vault, j1_down, ROW, "d", TODAY, meta)
    assert failed == ["j1"] and [s[2] for s in samples] == ["no"]

    def all_down(role, payload):
        raise ValueError("down")

    samples, failed = p3_research.run_judges(fx.vault, all_down, ROW, "d", TODAY, meta)
    assert samples == [] and failed == ["j1", "j2"]


# --- web-call cap -------------------------------------------------------------------------------------------


def test_web_calls_never_exceed_the_cap(fx):
    fx.cruxes = [_crux(r, n=2) for r in ("mechanism", "comparator", "horizon", "skeptic", "evidence", "evidence", "evidence", "evidence")]

    def gap(payload):
        ids = [c["crux_id"] for c in payload["cruxes"]]
        return {"market_id": payload["market_id"],
                "assessments": [{"crux_id": i, "status": "unanswered", "missing": "m"} for i in ids],
                "follow_up_queries": [{"crux_id": i, "query": f"again {i}"} for i in ids], "error": None}

    fx.gap = gap
    _, meta, _, _ = p3_research.build_evidence(fx.vault, fx.runner, ROW, TODAY, "full")
    assert len(fx.queries) <= meta["web_calls"] <= RESEARCH_MAX_WEB_CALLS == 12  # cache hits still spend budget
    assert meta["gap_queries"] == 3  # the gap round keeps its reserve even with 16 crux queries planned


def test_web_budget_reserve():
    b = p3_research.WebBudget(3)
    assert b.take(1) and b.take(1, reserve=1) and not b.take(1, reserve=1) and b.take(1) and not b.take(1)
    assert b.used == 3


# --- J2 flip, union range, offer evidence -------------------------------------------------------------------


def _complete(p, lo, hi, verdicts=()):
    return DeepResearcherComplete(
        status="complete", market_id="m1", estimated_p=p, p_low=lo, p_high=hi, confidence="medium",
        resolution_checklist=[], key_assumptions=[], as_of=TODAY, markdown="x",
        crux_verdicts=list(verdicts),
    )


def test_aggregate_two_judges_flip_and_union():
    j1 = (_complete(0.7, 0.6, 0.8), None, "yes")
    j2 = (_complete(0.2, 0.1, 0.3), None, "no")
    agg = aggregate_samples([j1, j2])
    assert agg["samples"] == pytest.approx([0.7, 0.8]) and agg["spread"] == pytest.approx(0.1)
    assert agg["p"] == pytest.approx(0.75) and agg["p_low"] == pytest.approx(0.6) and agg["p_high"] == pytest.approx(0.9)


def test_offer_evidence_only_verified_and_cited_lines(fx):
    rows = [
        {"query": "a", "crux_id": "c1", "role": "mechanism", "verified": [0],
         "research_data": "- up1 | https://a.example/1 | 2026-11-10\n- up2 | https://a.example/2 | 2026-11-10"},
        {"query": "b", "crux_id": "c2", "role": "skeptic", "verified": [0],
         "research_data": "- down1 | https://b.example/1 | 2026-11-10"},
        {"query": "c", "crux_id": "c3", "role": "evidence",  # never audited (e.g. gemini)
         "research_data": "- unaudited | https://c.example/1 | 2026-11-10"},
    ]
    verdicts = [
        {"crux_id": "c1", "direction": "yes", "cited": ["E1", "E2"]},  # E2 is unverified
        {"crux_id": "c2", "direction": "no", "cited": ["E3"]},
        {"crux_id": "c3", "direction": "yes", "cited": ["E4", "E99"]},  # unverified / non-existent
    ]
    out = p3_research.offer_evidence(rows, TODAY, [(_complete(0.6, 0.5, 0.7, verdicts), None, "yes")])
    assert out == {
        "evidence_bull": ["up1 | https://a.example/1 | 2026-11-10"],
        "evidence_bear": ["down1 | https://b.example/1 | 2026-11-10"],
    }


# --- end to end in stub mode: ledger + frontmatter shape ---------------------------------------------------


def _stub_world(monkeypatch, vault):
    monkeypatch.setenv("PMBOT_EVIDENCE_SOURCE", "web")  # the stub web_searcher answers (no A-IQ server in tests)
    from orchestrator import scraper
    from orchestrator.scraper import MarketRow

    vault.write_filter_log("0xstub", {"market_id": "0xstub", "passed": True, "trigger": None,
                                      "confidence_multiplier": 1.0, "details": "ok", "error": None})
    monkeypatch.setattr(
        scraper, "fetch_market_row",
        lambda mid: MarketRow(market_id=mid, market_title="Will it?", market_description="d",
                              market_data={"yes_price": 0.2, "liquidity": 50_000.0, "volume": 1_000.0,
                                           "days_to_resolution": 30, "end_date": "2026-12-31"}),
    )


def test_stub_runner_full_stage_ledger_and_frontmatter_shape(monkeypatch, vault):
    _stub_world(monkeypatch, vault)
    out = phases.phase3_qualitative_pipeline(vault, runner=spawn_agent)
    assert len(out) == 1
    fm = vault.read_market_record("0xstub", "active")
    assert fm["pipeline"] == "crux_v1" and fm["research_depth"] == "full"
    assert fm["cruxes_total"] == 5 and fm["crux_answered"] == 5 and fm["crux_unanswered"] == 0
    assert fm["gap_queries"] == 0 and fm["audit_dropped"] == 0
    assert fm["framing_gap"] == pytest.approx(0.0, abs=1e-6)
    assert fm["samples"] == pytest.approx([0.55, 0.55]) and fm["estimated_p"] == pytest.approx(0.55)
    assert fm["p_low"] == pytest.approx(0.51) and fm["p_high"] == pytest.approx(0.59)
    assert sum(fm["source_mix"].values()) == 5 and fm["web_calls"] == 6 and fm["pipeline_flags"] == []
    meta = vault.read_research_bundle_meta("0xstub")["crux_meta"]
    assert len(meta["cruxes"]) == 5 and meta["audit_unverified"] == 5
    # offers cite only audit-verified lines: the stub bundle has no page text, so there are none
    assert fm["evidence_bull"] == [] and fm["evidence_bear"] == []
    state = vault.read_filter_log("0xstub")["research_state"]
    assert state["pipeline"] == "crux_v1" and state["cruxes_total"] == 5
    # the same fields ride along to Phase 4
    assert out[0]["pipeline"] == "crux_v1" and out[0]["crux_answered"] == 5


def test_stub_error_mode_flags_the_market(monkeypatch, vault):
    monkeypatch.setenv("PMBOT_MODE", "stub_error")
    _stub_world(monkeypatch, vault)
    assert phases.phase3_qualitative_pipeline(vault, runner=spawn_agent) == []
    assert not [r for r in ledger.iter_rows(vault) if r["stage"] == "traded"]


# --- backtest entry --------------------------------------------------------------------------------------------


def test_backtest_research_uses_given_today_and_freshness_and_leaves_no_ledger(monkeypatch, tmp_path):
    from orchestrator import brave_search

    seen: dict[str, Any] = {}
    real = p3_research.build_evidence

    def spy(vault, runner, row, today, depth, **kw):
        seen["today"], seen["fresh"], seen["market_id"] = today, brave_search._FRESHNESS_OVERRIDE, row["market_id"]
        return real(vault, runner, row, today, depth, **kw)

    monkeypatch.setattr(p3_research, "build_evidence", spy)
    monkeypatch.setattr(p3_evidence, "fetch_research_bundle",
                        lambda qs: [{"query": q, "research_data": _line(i), "error": None} for i, q in enumerate(qs)])
    gamma = {"conditionId": "0xback", "question": "Will Y happen?", "description": "d",
             "endDate": "2026-03-01T00:00:00Z", "resolutionSource": "Src"}
    out = p3_research.backtest_research(gamma, "2026-01-15", "2025-12-16to2026-01-15")
    assert seen == {"today": "2026-01-15", "fresh": "2025-12-16to2026-01-15", "market_id": "0xback"}
    assert brave_search._FRESHNESS_OVERRIDE is None  # restored
    assert out["pipeline"] == "crux_v1" and 0.0 <= out["p_low"] <= out["p"] <= out["p_high"] <= 1.0
    assert out["cruxes_total"] >= 4
    assert not list(tmp_path.rglob("decisions.jsonl"))


# --- directives version bump -------------------------------------------------------------------------------


def test_directives_are_stamped_and_version_bumped_once_for_the_new_pipeline(tmp_path):
    from obsidian_utils import ObsidianManager
    from orchestrator.directives import directive_version, extract_filter_directives

    fresh = ObsidianManager(vault_base=tmp_path / "fresh")
    fresh.cold_start_protocol()
    assert "research_pipeline: crux_v1" in fresh.read_directives() and directive_version(fresh.read_directives()) == 1

    legacy = ObsidianManager(vault_base=tmp_path / "legacy")
    legacy.cold_start_protocol()
    text = legacy.read_directives().replace("research_pipeline: crux_v1\n", "")
    (legacy._dirs["system"] / "active_directives.md").write_text(text)
    before = extract_filter_directives(text)
    assert legacy.cold_start_protocol() is False
    after = legacy.read_directives()
    assert directive_version(after) == 2 and "research_pipeline: crux_v1" in after and "pipeline_note" in after
    assert extract_filter_directives(after) == before  # knobs untouched
    assert legacy.cold_start_protocol() is False and directive_version(legacy.read_directives()) == 2  # idempotent
