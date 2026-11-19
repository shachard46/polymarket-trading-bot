"""Review fixes on crux_v1: compound markets, source-quality cache, provenance, judge trimming, evidence lists,
web-fallback logging, judge-arm A/B. No network."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from orchestrator import compound, evidence, playbooks, source_quality
from orchestrator.agent_outputs import DeepResearcherComplete
from orchestrator.llm import usage
from orchestrator.phases import p3_research
from orchestrator.stats import compute_stats, render_stats
from tests.orchestrator.test_crux_pipeline import ROW, TODAY, Fx, _complete, _crux, fx  # noqa: F401
from tests.orchestrator.test_phase3_helpers import deep_researcher_complete

SPEC = {"type": "and", "components": [{"id": "A", "label": "X wins"}, {"id": "B", "label": "Y wins"}]}


# --- 2. compound markets ------------------------------------------------------------------------------------


def test_combine_and_or_and_correlation_bounds():
    assert compound.combine("and", [0.5, 0.4], 0.0) == pytest.approx(0.2)
    assert compound.combine("or", [0.5, 0.4], 0.0) == pytest.approx(0.7)
    assert compound.combine("and", [0.5, 0.4], 1.0) == pytest.approx(compound.combine("and", [0.5, 0.4], 0.9))  # clamped
    assert compound.combine("and", [0.5, 0.4], 0.9) == pytest.approx(0.1 * 0.2 + 0.9 * 0.4)
    assert compound.combine("or", [0.5, 0.4], 0.9) == pytest.approx(0.1 * 0.7 + 0.9 * 0.5)
    assert compound.combine("and", [0.5, 0.4], -2) == pytest.approx(0.2)  # negative correlation counts as 0
    with pytest.raises(ValueError):
        compound.combine("xor", [0.5, 0.4], 0)


def test_parse_spec_validation():
    assert compound.parse_spec(SPEC)["type"] == "and"
    assert compound.parse_spec({"type": "or", "components": ["A", "B", "B"]})["components"] == [
        {"id": "A", "label": "A"}, {"id": "B", "label": "B"}]
    for bad in (None, "x", {"type": "xor", "components": ["A", "B"]}, {"type": "and", "components": ["A"]}):
        assert compound.parse_spec(bad) is None


def _comp(p, comps, rho=0.0, framing="yes"):
    c = DeepResearcherComplete(
        status="complete", market_id="m1", estimated_p=p, p_low=max(0, p - 0.05), p_high=min(1, p + 0.05),
        confidence="medium", resolution_checklist=[], key_assumptions=[], as_of=TODAY, markdown="x",
        components=[{"id": k, "p": v} for k, v in comps.items()], correlation=rho,
    )
    return (c, None, framing)


def test_check_gap_and_framing_flip_and_missing_components():
    ok = compound.check([_comp(0.20, {"A": 0.5, "B": 0.4})], SPEC)
    assert ok["compound_p"] == pytest.approx(0.2) and ok["compound_gap"] == pytest.approx(0.0)
    bad = compound.check([_comp(0.60, {"A": 0.5, "B": 0.4})], SPEC)  # judge says 0.60, parts give 0.20
    assert bad["compound_gap"] == pytest.approx(0.4)
    flipped = compound.check([_comp(0.80, {"A": 0.5, "B": 0.4}, framing="no")], SPEC)  # P(NO)=0.8 -> P(YES)=0.2
    assert flipped["compound_gap"] == pytest.approx(0.0)
    none = compound.check([_comp(0.3, {"A": 0.5})], SPEC)
    assert none["compound_gap"] is None and none["compound_p"] is None


def test_architect_compound_tags_cruxes_and_adds_missing_component_crux(fx):
    inner = fx.runner

    def runner(role, payload):
        if role == "briefer":
            return {"market_id": "m1", "compound": SPEC, "error": None, "cruxes": [
                {**_crux("mechanism"), "component": "A"}, _crux("comparator"), _crux("skeptic"), _crux("horizon"),
                {**_crux("evidence"), "component": "ZZ"},  # unknown component id: dropped to None
            ]}
        return inner(role, payload)

    _, meta, _, _ = p3_research.build_evidence(fx.vault, runner, ROW, TODAY, "full")
    assert meta["compound"]["type"] == "and"
    comps = [c.get("component") for c in meta["cruxes"]]
    assert "A" in comps and "B" in comps and "ZZ" not in comps  # B got a template crux
    samples, _ = p3_research.run_judges(fx.vault, runner, ROW, "d", TODAY, meta)
    payload = fx.of("deep_researcher")[0]
    assert payload["compound"]["components"][1]["id"] == "B"
    assert all("components" in s[0].model_dump() for s in samples)


def test_compound_inconsistent_skips_and_ledgers(monkeypatch, vault):
    from orchestrator import ledger, phases
    from orchestrator.runner import spawn_agent
    from tests.orchestrator.test_crux_pipeline import _stub_world
    from orchestrator import runner_stubs

    _stub_world(monkeypatch, vault)
    monkeypatch.setitem(runner_stubs.STUB_RESPONSES, "briefer", _briefer_with_compound)
    monkeypatch.setitem(runner_stubs.STUB_RESPONSES, "deep_researcher", _judge_inconsistent)
    out = phases.phase3_qualitative_pipeline(vault, runner=spawn_agent)
    assert out == []
    rows = [r for r in ledger.iter_rows(vault) if r["stage"] == "research_skip"]
    assert rows[-1]["reason"] == "compound_inconsistent"
    assert rows[-1]["compound_gap"] == pytest.approx(0.35) and rows[-1]["compound_type"] == "and"


def _briefer_with_compound(payload):
    slots = ("mechanism", "comparator", "evidence", "horizon", "skeptic")
    return {
        "market_id": payload["market_id"], "error": None, "compound": SPEC,
        "cruxes": [{"role": r, "question": f"{r}?", "queries": [f"{r} q as of {payload['today']}"],
                    "moves_p_if": "x", "component": "A" if r == "evidence" else None} for r in slots],
    }


def _judge_inconsistent(payload):
    from orchestrator.runner_stubs import _deep_researcher_stub
    out = _deep_researcher_stub(payload)
    out["components"] = [{"id": "A", "p": 0.5}, {"id": "B", "p": 0.4}]  # combined 0.20; stub says 0.55 / 0.45 flipped
    out["correlation"] = 0.0
    return out


# --- 3. source-quality cache -------------------------------------------------------------------------------


def test_lookup_order_yaml_over_cache_over_heuristic(tmp_path):
    cache = tmp_path / "c.json"
    source_quality.bind_cache(cache)
    source_quality.store({"reuters.com": "blog", "newsite.example": "major", "agency.gov": "blog", "x.org": "aggregator"})
    assert source_quality.quality_for("https://www.reuters.com/a") == "major"  # yaml wins over the cache
    assert source_quality.quality_for("https://newsite.example/a") == "major"  # cache
    assert source_quality.quality_for("https://agency.gov/a") == "blog"  # cache wins over the heuristic
    assert source_quality.quality_for("https://other.gov/a") == "official"  # heuristic
    assert source_quality.quality_for("https://nobody.example/a") == "unknown"
    entry = json.loads(cache.read_text())["newsite.example"]
    assert entry["tier"] == "major" and entry["by"] == "source_classifier" and entry["ts"]


def test_heuristics_wires_statistics_central_banks_courts():
    for host in ("bundesbank.de", "www.census.gov", "supremecourt.uk", "ecb.europa.eu", "ons.gov.uk", "stats.gov.cn"):
        assert source_quality.heuristic_tier(host) == "official", host
    for host in ("afp.com", "www.apnews.com", "x.bloomberg.com"):
        assert source_quality.heuristic_tier(host) == "major", host
    assert source_quality.heuristic_tier("reasons.com") == "unknown"


def test_unresolved_domains_and_domain_key(tmp_path):
    source_quality.bind_cache(tmp_path / "c.json")
    assert source_quality.domain_key("news.bbc.co.uk") == "bbc.co.uk" and source_quality.domain_key("a.b.example.com") == "example.com"
    urls = ["https://a.example.com/1", "https://b.example.com/2", "https://reuters.com/x", "https://x.gov/y", "https://z.example.net/"]
    assert source_quality.unresolved_domains(urls) == ["example.com", "example.net"]
    source_quality.store({"example.com": "unknown"})  # an unknown answer is cached too: never re-asked
    assert source_quality.unresolved_domains(urls) == ["example.net"]


def test_classifier_called_once_per_research_and_never_for_cached(fx, monkeypatch):
    def fetch_unknown(queries):
        return [{"query": q, "research_data": "- f | https://mystery.example/p | 2026-11-10", "error": None} for q in queries]

    from orchestrator.phases import p3_evidence

    monkeypatch.setattr(p3_evidence, "fetch_research_bundle", fetch_unknown)
    calls = []
    inner = fx.runner

    def runner(role, payload):
        if role == "source_classifier":
            calls.append(payload["domains"])
            return {"market_id": payload["market_id"], "domains": [{"domain": d, "tier": "blog"} for d in payload["domains"]],
                    "error": None}
        return inner(role, payload)

    p3_research.build_evidence(fx.vault, runner, ROW, TODAY, "full")
    assert calls == [["mystery.example"]]  # one batched call, one domain
    p3_research.build_evidence(fx.vault, runner, {**ROW, "market_id": "m2"}, TODAY, "full")
    assert len(calls) == 1  # cached: not asked again
    assert source_quality.quality_for("https://mystery.example/p") == "blog"
    assert (fx.vault._dirs["system"] / "source_quality_cache.json").exists()


def test_classifier_failure_is_flagged_not_cached(fx, monkeypatch):
    from orchestrator.phases import p3_evidence

    monkeypatch.setattr(p3_evidence, "fetch_research_bundle", lambda qs: [
        {"query": q, "research_data": "- f | https://mystery.example/p | 2026-11-10", "error": None} for q in qs])
    inner = fx.runner

    def runner(role, payload):
        if role == "source_classifier":
            raise ValueError("down")
        return inner(role, payload)

    _, meta, _, _ = p3_research.build_evidence(fx.vault, runner, ROW, TODAY, "full")
    assert "source_classifier_failed" in meta["flags"]
    assert source_quality.unresolved_domains(["https://mystery.example/p"]) == ["mystery.example"]


# --- 4. provenance + playbooks ------------------------------------------------------------------------------


def test_provenance_question_becomes_a_crux_query_and_reaches_the_judge(fx):
    fx.cruxes = [{**_crux("evidence"), "provenance_question": "Who ran the poll and what is their record?"},
                 _crux("mechanism"), _crux("comparator"), _crux("skeptic"), _crux("horizon")]
    _, meta, _, _ = p3_research.build_evidence(fx.vault, fx.runner, ROW, TODAY, "full")
    c = next(c for c in meta["cruxes"] if c.get("provenance_question"))
    assert any("who ran the poll" in q.lower() and "as of" in q.lower() for q in fx.queries)
    p3_research.run_judges(fx.vault, fx.runner, ROW, "d", TODAY, meta)
    table = fx.of("deep_researcher")[0]["crux_table"]
    assert any(t.get("provenance_question") == c["provenance_question"] for t in table)


def test_playbook_registry_is_empty_and_flows_when_set(fx, monkeypatch):
    assert playbooks.PLAYBOOKS == {} and playbooks.playbook_for("Politics") is None
    row = {**ROW, "market_data": {**ROW["market_data"], "category": "Politics"}}
    p3_research.build_evidence(fx.vault, fx.runner, row, TODAY, "full")
    assert "playbook" not in fx.of("briefer")[0]
    monkeypatch.setitem(playbooks.PLAYBOOKS, "politics", "be careful")
    p3_research.build_evidence(fx.vault, fx.runner, {**row, "market_id": "m2"}, TODAY, "full")
    assert fx.of("briefer")[-1]["playbook"] == "be careful"


# --- 5. judge input trimming --------------------------------------------------------------------------------


def _meta(statuses):
    return {"cruxes": [{"crux_id": f"c{i}", "status": s, "followed_up": False} for i, s in enumerate(statuses, 1)]}


def test_select_judge_ids_top_k_by_tier_then_recency_and_partial_for_unanswered():
    rows = [
        {"query": "a", "crux_id": "c1", "research_data": "\n".join([
            "- old official | https://x.gov/1 | 2026-01-01", "- blog new | https://a.blogspot.com/1 | 2026-11-18",
            "- major new | https://reuters.com/1 | 2026-11-18", "- major old | https://reuters.com/2 | 2026-06-01",
            "- agg | https://en.wikipedia.org/x | 2026-11-18", "- unknown | https://q.example/1 | 2026-11-18"])},
        {"query": "b", "crux_id": "c2", "research_data": "- hidden | https://reuters.com/3 | 2026-11-18"},
    ]
    keep = p3_research.select_judge_ids(rows, _meta(["answered", "unanswered"]), TODAY, k=4)
    # E1 official, E3 major new, E4 major old, E5 aggregator; the unknown (E6) and blog (E2) lose;
    # c2 is unanswered: its (single) line is sent as partial evidence
    assert keep == {"E1", "E3", "E4", "E5", "E7"}
    assert p3_research.select_judge_ids(rows, None, TODAY) is None
    table = p3_research.crux_table(rows, _meta(["answered", "unanswered"]), TODAY, keep)
    assert table[0]["evidence_ids"] == ["E1", "E3", "E4", "E5"] and table[1]["evidence_ids"] == ["E7"]
    assert table[1]["evidence_partial"] is True and "evidence_partial" not in table[0]
    many = rows + [{"query": "c", "crux_id": "c2", "research_data": "\n".join(
        f"- more {i} | https://reuters.com/m{i} | 2026-11-1{i}" for i in range(5))}]
    k2 = p3_research.select_judge_ids(many, _meta(["answered", "unanswered"]), TODAY, k=4)
    c2_ids = {r["id"] for r in evidence.label_lines(many, TODAY) if r["crux_id"] == "c2"}
    assert len(k2 & c2_ids) == 2  # at most 2 partial lines for the unanswered crux


def test_label_bundle_only_ids_keeps_global_ids():
    rows = [{"query": "a", "crux_id": "c1", "research_data": "- one | https://a.example/1 | 2026-11-10\n- two | https://a.example/2 | 2026-11-10"},
            {"query": "b", "crux_id": "c2", "research_data": "- three | https://a.example/3 | 2026-11-10"}]
    out = evidence.label_bundle(rows, TODAY, only_ids={"E2"})
    assert len(out) == 1 and out[0]["research_data"].startswith("[E2 |") and "one" not in out[0]["research_data"]


def test_judges_receive_trimmed_bundle_and_input_is_logged(fx, caplog):
    fx.cruxes = [_crux(r, n=2) for r in ("mechanism", "comparator", "skeptic", "horizon", "evidence")]
    fx.gap = lambda p: {"market_id": "m1", "error": None, "follow_up_queries": [],
                        "assessments": [{"crux_id": c["crux_id"], "status": "unanswered" if c["crux_id"] == "c5" else "answered",
                                         "missing": ""} for c in p["cruxes"]]}
    _, meta, _, _ = p3_research.build_evidence(fx.vault, fx.runner, ROW, TODAY, "full")
    with caplog.at_level(logging.INFO):
        p3_research.run_judges(fx.vault, fx.runner, ROW, "d", TODAY, meta)
    msg = next(r.getMessage() for r in caplog.records if "judge input lines" in r.getMessage())
    before, after = (int(x) for x in msg.split(": ")[1].split(" -> "))
    assert after <= before
    sent = fx.of("deep_researcher")[0]
    ids = [ln.split(" ")[0][1:] for r in sent["research_bundle"] for ln in r["research_data"].splitlines() if ln.startswith("[E")]
    assert len(ids) == after
    c5 = next(t for t in sent["crux_table"] if t["crux_id"] == "c5")
    assert c5["status"] == "unanswered" and len(c5["evidence_ids"]) == 2 and c5["evidence_partial"] is True


# --- 6. small fixes -----------------------------------------------------------------------------------------


def test_offer_evidence_falls_back_to_unverified_cited_lines():
    rows = [{"query": "a", "crux_id": "c1", "research_data": "- up | https://a.example/1 | 2026-11-10"},
            {"query": "b", "crux_id": "c2", "verified": [0], "research_data": "- down | https://b.example/1 | 2026-11-10"}]
    verdicts = [{"crux_id": "c1", "direction": "yes", "cited": ["E1"]}, {"crux_id": "c2", "direction": "no", "cited": ["E2"]}]
    out = p3_research.offer_evidence(rows, TODAY, [(_complete(0.6, 0.5, 0.7, verdicts), None, "yes")])
    assert out["evidence_bull"] == ["up | https://a.example/1 | 2026-11-10 (unverified)"]
    assert out["evidence_bear"] == ["down | https://b.example/1 | 2026-11-10"]


def test_web_fallback_logs_real_reasons_and_per_row_backend(monkeypatch, caplog):
    from orchestrator import config

    monkeypatch.setattr(config, "web_backend", lambda: config.WEB_BACKEND_BRAVE)
    monkeypatch.setattr(evidence, "_brave_rows", lambda r, m, qs, t: [
        {"query": qs[0], "research_data": "- ok | https://reuters.com/1 | 2026-11-10", "error": None},
        {"query": qs[1], "research_data": "", "error": "web_extractor parse error: bad json"},
        {"query": qs[2], "research_data": "", "error": "brave: HTTP 429"}])
    monkeypatch.setattr(evidence, "_gemini_rows", lambda r, m, qs, t: [
        {"query": q, "research_data": "- g | https://reuters.com/2 | 2026-11-10", "error": None} for q in qs])
    with caplog.at_level(logging.INFO):
        evidence.fetch_web_rows(lambda *a: None, "m1", ["a", "b", "c"], TODAY)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "extractor: web_extractor parse error: bad json" in text and "search: brave: HTTP 429" in text
    assert "backend=brave+gemini(fallback=2)" in text
    assert evidence._backend_label(["gemini", "gemini"]) == "gemini" and evidence._backend_label(["brave"]) == "brave"


def test_brave_guard_logs_when_extractor_lines_all_dropped(monkeypatch, caplog):
    from orchestrator import brave_search

    page = brave_search.Page(url="https://a.example/1", title="t", page_age=None, text="x", fetched=True)
    monkeypatch.setattr(brave_search, "search_pages", lambda q: [page])
    runner = lambda role, payload: {"market_id": "m1", "results": [  # noqa: E731
        {"query": "q", "research_data": "- fact | https://elsewhere.example/9 | 2026-11-10", "error": None}], "error": None}
    with caplog.at_level(logging.WARNING):
        row = evidence._brave_one(runner, "m1", "q", TODAY)
    assert row["research_data"] == "NOT_FOUND" and any("WEB-GUARD" in r.getMessage() for r in caplog.records)


# --- 7. judge arm A/B ---------------------------------------------------------------------------------------


def test_judge_arm_is_deterministic_and_modes(monkeypatch):
    monkeypatch.setenv("JUDGE_ARM_MODE", "ab")
    arms = {evidence.judge_arm(f"0x{i}") for i in range(40)}
    assert arms == {"one", "two"} and evidence.judge_arm("0x7") == evidence.judge_arm("0x7")
    assert {evidence.judge_framing(f"0x{i}") for i in range(40)} == {"yes", "no"}
    monkeypatch.setenv("JUDGE_ARM_MODE", "one")
    assert evidence.judge_arm("0x7") == "one"
    monkeypatch.setenv("JUDGE_ARM_MODE", "two")
    assert evidence.judge_arm("0x7") == "two"
    monkeypatch.setenv("JUDGE_ARM_MODE", "bogus")
    with pytest.raises(ValueError):
        evidence.judge_arm("0x7")


@pytest.mark.parametrize("framing", ["yes", "no"])
def test_one_arm_runs_one_judge_with_the_hash_framing_and_flips_in_code(fx, monkeypatch, framing):
    monkeypatch.setenv("JUDGE_ARM_MODE", "one")
    monkeypatch.setattr(evidence, "judge_framing", lambda mid: framing)
    p3_research.build_evidence(fx.vault, fx.runner, ROW, TODAY, "full")
    meta = fx.vault.read_research_bundle_meta("m1")["crux_meta"]
    samples, failed = p3_research.run_judges(fx.vault, fx.runner, ROW, "d", TODAY, meta)
    assert failed == [] and len(samples) == 1 and len(fx.of("deep_researcher")) == 1
    assert fx.of("deep_researcher")[0]["ask"] == framing
    from orchestrator.phases.p3_samples import aggregate_samples
    assert aggregate_samples(samples)["p"] == pytest.approx(0.6) and aggregate_samples(samples)["spread"] == 0.0
    assert p3_research.failed_judges("m1", samples) == []
    assert p3_research.failed_judges("m1", []) == ["j1"]


def test_one_arm_end_to_end_ledger_and_frontmatter(monkeypatch, vault):
    from orchestrator import ledger, phases
    from orchestrator.runner import spawn_agent
    from tests.orchestrator.test_crux_pipeline import _stub_world

    monkeypatch.setenv("JUDGE_ARM_MODE", "one")
    monkeypatch.setattr(evidence, "judge_framing", lambda mid: "no")
    _stub_world(monkeypatch, vault)
    out = phases.phase3_qualitative_pipeline(vault, runner=spawn_agent)
    assert len(out) == 1 and out[0]["judge_arm"] == "one"
    fm = vault.read_market_record("0xstub", "active")
    assert fm["judge_arm"] == "one" and fm["judge_framing"] == "no" and fm["framing_gap"] is None
    assert fm["samples"] == pytest.approx([0.55]) and fm["estimated_p"] == pytest.approx(0.55)
    assert fm["pipeline_flags"] == []


def test_usage_rows_are_tagged_with_the_judge_arm(tmp_path, monkeypatch):
    monkeypatch.setenv("PMBOT_VAULT_PATH", str(tmp_path))
    from tests.orchestrator.test_llm_usage import _resp

    with usage.judge_arm_scope("one"):
        usage.record("deep_researcher", _resp())
    usage.record("deep_researcher", _resp())
    rows = usage.iter_usage(tmp_path)
    assert rows[0]["judge_arm"] == "one" and rows[1]["judge_arm"] is None


def test_stats_brier_and_cost_by_judge_arm():
    ledger_rows = []
    for m, arm, p, y in (("a", "one", 0.8, 1), ("b", "one", 0.6, 0), ("c", "two", 0.9, 1)):
        ledger_rows += [
            {"ts": "t", "market_id": m, "stage": "traded", "p": p, "q": 0.5, "judge_arm": arm},
            {"ts": "t", "market_id": m, "stage": "resolution", "outcome_yes": y},
        ]
    use = [{"market_id": "a", "judge_arm": "one", "cost_usd": 0.02, "role": "deep_researcher"},
           {"market_id": "b", "judge_arm": "one", "cost_usd": 0.04, "role": "deep_researcher"},
           {"market_id": "c", "judge_arm": "two", "cost_usd": 0.10, "role": "deep_researcher"},
           {"market_id": "c", "judge_arm": None, "cost_usd": 1.0, "role": "briefer"}]
    s = compute_stats(ledger_rows, use)
    assert s["brier_by_judge_arm"]["one"][0] == 2 and s["brier_by_judge_arm"]["two"][0] == 1
    n, total, per = s["cost"]["judge_arm_cost"]["one"]
    assert n == 2 and total == pytest.approx(0.06) and per == pytest.approx(0.03)
    assert s["cost"]["judge_arm_cost"]["two"][2] == pytest.approx(0.10)
    text = render_stats(s)
    assert "Judge arms (1 vs 2 judges)" in text and "| one | 2 |" in text


def test_stub_tick_end_to_end_default_ab_arm_and_stats(tmp_path, monkeypatch):
    """A full stub tick with the shipped default JUDGE_ARM_MODE (ab): arm in ledger, stats.md has the judge-arm table."""
    from obsidian_utils import ObsidianManager
    from orchestrator import ledger, loop, polymarket_api, scraper
    from orchestrator.phases import p3_evidence
    from orchestrator.scraper import MarketRow
    from orchestrator.stats import write_stats

    monkeypatch.delenv("JUDGE_ARM_MODE")
    monkeypatch.setenv("PMBOT_MODE", "stub")
    monkeypatch.setenv("PMBOT_VAULT_PATH", str(tmp_path))
    md = {"yes_price": 0.26, "no_price": 0.74, "volume": 1_000.0, "liquidity": 20_000.0, "days_to_resolution": 30}
    rows = {"ok1": MarketRow(market_id="ok1", market_title="Will ok1?", market_description="d", market_data=md)}
    monkeypatch.setattr(scraper, "fetch_target_markets", lambda knobs=None: list(rows.values()))
    monkeypatch.setattr(scraper, "fetch_market_row", lambda mid: rows.get(mid))
    monkeypatch.setattr(scraper, "fetch_resolution", lambda mid: None)
    monkeypatch.setattr(polymarket_api, "fetch_holder_concentration", lambda cid: None)
    monkeypatch.setattr(p3_evidence, "fetch_research_bundle",
                        lambda qs: [{"query": q, "research_data": "stub data", "error": None} for q in qs])
    vault = ObsidianManager()
    vault.cold_start_protocol()
    loop.run_pipeline_tick(vault)
    loop.run_pipeline_tick(vault)
    traded = [r for r in ledger.iter_rows(vault) if r["market_id"] == "ok1" and r["stage"] == "traded"][0]
    assert traded["judge_arm"] == evidence.judge_arm("ok1")
    assert traded["prior_kind"] == "judgment" and traded["prior"] == 0.5 and traded.get("base_rate") is None
    assert traded.get("judge_framing") == (evidence.judge_framing("ok1") if traded["judge_arm"] == "one" else None)
    write_stats(vault)
    stats_text = (vault._dirs["system"] / "stats.md").read_text()
    assert "Judge arms (1 vs 2 judges)" in stats_text and "### Prior kinds" in stats_text


# --- 1. prompt rules ----------------------------------------------------------------------------------------


def test_judge_prompt_states_the_anchoring_rules():
    import agents_blueprint

    prompt = agents_blueprint.AGENTS["deep_researcher"]["system_prompt"]
    assert "never move the estimate" in prompt and "toward 0.5" in prompt and "## Base Rate Movement" in prompt
    assert "discounted_sources" in prompt and "components" in prompt
    briefer = agents_blueprint.AGENTS["briefer"]["system_prompt"]
    assert "provenance_question" in briefer and '"compound"' in briefer
