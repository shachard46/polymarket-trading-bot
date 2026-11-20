"""Parser tolerance, judge prior vs base rate, comparator gap query, quick-stage logging. No network."""

from __future__ import annotations

import json
import logging

import pytest

from obsidian_utils.schemas import ResearchFrontmatter
from orchestrator import runner as runner_mod
from orchestrator.agent_outputs import DeepResearcherComplete
from orchestrator.parse import parse_agent_json_or_yaml
from orchestrator.phases import p3_quick, p3_research
from orchestrator.stats import compute_stats, render_stats
from tests.orchestrator.test_crux_pipeline import ROW, TODAY, Fx, fx  # noqa: F401  (fixture)
from tests.orchestrator.test_phase3_helpers import deep_researcher_complete

WE_KEYS = ["market_id", "results", "error"]
WE_BODY = json.dumps({
    "market_id": "0x1",
    "results": [{"query": "q", "research_data": "NOT_FOUND", "error": None}],
    "error": None,
})


# --- 1. parser ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    ">>web_extractor_response}```json\n" + WE_BODY + "\n```",  # the live shape
    ">>web_extractor_response}```json\n" + WE_BODY,  # no closing fence
    ">>web_extractor_response}```json\nerror: x\nmarket_id: y\n" + WE_BODY,  # YAML-looking junk first
    "<<<RESPONSE>>>\n" + WE_BODY + "\n<<<END>>>",
    "Here is the result: " + WE_BODY + " Hope that helps.",
    "```json\n" + WE_BODY + "\n```",
    WE_BODY,
])
@pytest.mark.parametrize("keys", [WE_KEYS, None])
def test_parser_ignores_wrapper_garbage(raw, keys):
    parsed = parse_agent_json_or_yaml(raw, expected_keys=keys)
    assert sorted(parsed) == ["error", "market_id", "results"]
    assert parsed["market_id"] == "0x1"


def test_parser_prefers_object_matching_expected_schema():
    raw = 'note {"foo": 1} then {"market_id": "m", "results": [], "error": null}'
    assert parse_agent_json_or_yaml(raw, expected_keys=WE_KEYS)["market_id"] == "m"
    assert parse_agent_json_or_yaml(raw) == {"foo": 1}  # no schema: first object wins


def test_parser_zero_overlap_keeps_old_behaviour():
    raw = 'prefix {"zzz": 1}'
    assert parse_agent_json_or_yaml(raw, expected_keys=WE_KEYS) == parse_agent_json_or_yaml(raw) == {"zzz": 1}


def test_parser_unwraps_single_key_wrapper_with_expected_keys():
    raw = json.dumps({"web_extractor_response": {"market_id": "x", "results": [], "error": None}})
    assert parse_agent_json_or_yaml(raw, expected_keys=WE_KEYS)["market_id"] == "x"
    assert list(parse_agent_json_or_yaml(raw)) == ["web_extractor_response"]  # no schema: unchanged


def test_schema_mismatch_logs_raw_prefix(caplog, monkeypatch):
    spec = {"output_model": object(), "output_schema": {"market_id": "string"}}

    def boom(*a, **k):
        raise runner_mod.AgentSchemaError("web_extractor", "output", "bad")

    monkeypatch.setattr(runner_mod, "validate_payload", boom)
    raw = ">>marker}```json\n" + json.dumps({"market_id": "m", "bogus": 1})
    with caplog.at_level(logging.WARNING, logger="orchestrator.runner"):
        with pytest.raises(runner_mod.AgentSchemaError):
            runner_mod._validate_structured("web_extractor", spec, raw, {})
    assert "schema mismatch" in caplog.text and ">>marker}" in caplog.text


# --- 2. prior vs base rate --------------------------------------------------------------------------------


def _complete(**extra) -> DeepResearcherComplete:
    return DeepResearcherComplete.model_validate({**deep_researcher_complete({"market_id": "m1"}), **extra})


def test_normalise_reference_class_ok():
    f = p3_research.normalise_prior(_complete(
        base_rate=0.3, base_rate_n=12, base_rate_source="E7", prior=0.35, prior_kind="reference_class"))
    assert f["prior"] == 0.3 and f["prior_kind"] == "reference_class"  # prior equals base_rate
    assert (f["base_rate"], f["base_rate_n"], f["base_rate_source"]) == (0.3, 12, "E7") and not f["flags"]


def test_normalise_bad_kind_and_downgrade():
    f = p3_research.normalise_prior(_complete(prior=0.5, prior_kind="vibes"))
    assert f["prior_kind"] == "judgment" and "prior_kind_invalid" in f["flags"]
    f = p3_research.normalise_prior(_complete(prior=0.4, prior_kind="reference_class"))
    assert f["prior_kind"] == "judgment" and "prior_downgraded" in f["flags"] and f["prior"] == 0.4
    f = p3_research.normalise_prior(_complete(prior=0.5, prior_kind="structural"))
    assert f["prior_kind"] == "structural" and not f["flags"]


def test_base_rate_without_counts_is_dropped():
    f = p3_research.normalise_prior(_complete(base_rate=0.3, prior=0.3, prior_kind="reference_class"))
    assert f["base_rate"] is None and "base_rate_unsupported" in f["flags"]
    assert f["prior_kind"] == "judgment"


def test_missing_prior_is_flagged():
    f = p3_research.normalise_prior(_complete())
    assert f["prior"] is None and f["prior_kind"] == "judgment"
    assert {"prior_kind_missing", "prior_missing"} <= set(f["flags"])


def test_prior_fields_use_j1_and_flip_a_lone_no_judge():
    j1 = _complete(prior=0.3, prior_kind="structural")
    j2 = _complete(prior=0.6, prior_kind="judgment")
    out = p3_research.prior_fields([(j1, None, "yes"), (j2, None, "no")])
    assert out["prior"] == 0.3 and out["prior_kind"] == "structural"
    assert out["prior_j2"] == pytest.approx(0.4) and out["prior_kind_j2"] == "judgment"
    only_no = p3_research.prior_fields([(j2, None, "no")])
    assert only_no["prior"] == pytest.approx(0.4) and "prior_j2" not in only_no
    assert p3_research.prior_fields([]) == {}


def test_frontmatter_accepts_prior_fields():
    fm = ResearchFrontmatter(market_id="m", estimated_p=0.5, prior=0.4, prior_kind="structural",
                             base_rate=None, base_rate_n=None)
    assert fm.prior == 0.4 and fm.prior_kind == "structural"
    from orchestrator.phases.p4_execution import _CRUX_KEYS
    assert {"prior", "prior_kind", "base_rate", "base_rate_n"} <= set(_CRUX_KEYS)


def test_stats_prior_kinds_table():
    rows = []
    for m, kind, p, y in (("a", "judgment", 0.8, 1), ("b", "judgment", 0.6, 0), ("c", "structural", 0.9, 1)):
        rows += [
            {"ts": "t", "market_id": m, "stage": "traded", "p": p, "q": 0.5, "prior_kind": kind},
            {"ts": "t", "market_id": m, "stage": "resolution", "outcome_yes": y},
        ]
    rows.append({"ts": "t", "market_id": "d", "stage": "research_skip", "prior_kind": "reference_class"})
    s = compute_stats(rows, [])
    assert s["brier_by_prior_kind"]["judgment"][0] == 2
    assert s["researched_by_prior_kind"] == {"judgment": 2, "structural": 1, "reference_class": 1}
    text = render_stats(s)
    assert "### Prior kinds" in text and "| judgment | 2 | 2 |" in text and "| reference_class | 1 | 0 |" in text


def test_stub_judge_output_has_prior():
    out = deep_researcher_stub_judge()
    assert out["prior"] == 0.5 and out["prior_kind"] == "judgment" and out["base_rate"] is None


def deep_researcher_stub_judge():
    from orchestrator.runner_stubs import _deep_researcher_stub
    return _deep_researcher_stub({"market_id": "m", "mode": "judge", "ask": "yes"})


# --- 3. comparator gap query ----------------------------------------------------------------------------


def _gap_fn(statuses, follow):
    def gap(payload):
        return {
            "market_id": payload["market_id"],
            "assessments": [{"crux_id": c["crux_id"], "status": statuses(c), "missing": "m"} for c in payload["cruxes"]],
            "follow_up_queries": follow(payload),
            "error": None,
        }
    return gap


def test_comparator_followup_added_when_analyzer_omits_it(fx):
    fx.gap = _gap_fn(lambda c: "unanswered" if c["role"] == "comparator" else "answered", lambda p: [])
    _, meta, _, _ = p3_research.build_evidence(fx.vault, fx.runner, ROW, TODAY, "full")
    assert meta["gap_queries"] == 1
    gap_q = [q for q in fx.queries if q.startswith("historical results, how often")]
    assert len(gap_q) == 1 and "as of 2026-11-19" in gap_q[0].lower()
    comp = next(c for c in meta["cruxes"] if c["role"] == "comparator")
    assert comp["followed_up"]


def test_comparator_followup_goes_first_and_respects_cap(fx):
    def follow(p):  # three proposals for other unanswered cruxes
        ids = [c["crux_id"] for c in p["cruxes"] if c["role"] != "comparator"][:3]
        return [{"crux_id": i, "query": f"reworded {i}"} for i in ids]

    fx.gap = _gap_fn(lambda c: "unanswered", follow)
    _, meta, _, _ = p3_research.build_evidence(fx.vault, fx.runner, ROW, TODAY, "full")
    assert meta["gap_queries"] == 3
    gap_rows = [r for r in fx.vault.read_research_bundle("m1") if r.get("gap")]
    assert gap_rows[0]["role"] == "comparator" and len(gap_rows) == 3
    follow_qs = [q for q in fx.queries if q.startswith("reworded")]
    assert len(follow_qs) == 2  # lowest-priority analyzer query replaced


def test_comparator_proposed_by_analyzer_is_not_duplicated(fx):
    def follow(p):
        c = next(c for c in p["cruxes"] if c["role"] == "comparator")
        return [{"crux_id": c["crux_id"], "query": "analyzer history query"}]

    fx.gap = _gap_fn(lambda c: "unanswered" if c["role"] == "comparator" else "answered", follow)
    p3_research.build_evidence(fx.vault, fx.runner, ROW, TODAY, "full")
    assert not [q for q in fx.queries if q.startswith("historical results, how often")]
    assert [q for q in fx.queries if q.startswith("analyzer history query")]


# --- 4. quick log ---------------------------------------------------------------------------------------


def _quick_row():
    return {"market_id": "0xabcdef1234567890", "market_title": "Will X?", "market_description": "d",
            "market_data": {"yes_price": 0.42, "no_price": 0.58, "end_date": "2026-12-31"}}


@pytest.mark.parametrize("p, low, high, expect", [
    (0.43, 0.40, 0.46, "skip:no_divergence"),
    (0.70, 0.60, 0.80, "escalate_full"),
])
def test_quick_stage_logs_one_line(vault, caplog, p, low, high, expect):
    row = _quick_row()
    vault.write_filter_log(row["market_id"], {"market_id": row["market_id"], "passed": True, "trigger": None,
                                              "confidence_multiplier": 1.0, "details": "ok", "error": None})
    runner = lambda role, payload: deep_researcher_complete(payload, estimated_p=p, p_low=low, p_high=high)  # noqa: E731
    with caplog.at_level(logging.INFO):
        p3_quick._quick_stage(vault, runner, row, vault.read_directives(), TODAY)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[QUICK]")]
    assert len(lines) == 1
    assert lines[0].startswith("[QUICK] 0xabcdef12:") and f"p={p:.3f}" in lines[0]
    assert f"range={low:.3f}-{high:.3f}" in lines[0] and "q=0.420" in lines[0] and lines[0].endswith(expect)


def test_j2_prior_and_base_rate_are_flipped_to_yes_terms():
    j1 = _complete(prior=0.3, prior_kind="reference_class", base_rate=0.3, base_rate_n=10, base_rate_source="E1")
    j2 = _complete(prior=0.8, prior_kind="reference_class", base_rate=0.8, base_rate_n=10, base_rate_source="E1")
    out = p3_research.prior_fields([(j1, None, "yes"), (j2, None, "no")])
    assert out["base_rate"] == 0.3 and out["prior_j2"] == pytest.approx(0.2)
    only_no = p3_research.prior_fields([(j2, None, "no")])
    assert only_no["prior"] == pytest.approx(0.2) and only_no["base_rate"] == pytest.approx(0.2)
    assert only_no["base_rate_n"] == 10


def test_hub_builds_frontmatter_and_strips_model_frontmatter():
    from orchestrator.phases.agent_call import build_research_markdown
    from orchestrator.research import parse_deep_researcher
    md = ('---\nmarket_id: "WRONG"\nestimated_p: 0.99\np_low: 0.9\np_high: 1.0\nerror: null\n---\n\n'
          "## Bull Thesis\n\n- a\n\n## Bear Thesis\n\n- b\n\n## Post-Mortem\n\nstray text\n")
    c = _complete(markdown=md, market_id="0xabc", estimated_p=0.4, p_low=0.3, p_high=0.5)
    out = build_research_markdown(c)
    assert out.count("---") == 2 and "WRONG" not in out and "stray" not in out
    r = parse_deep_researcher(out)
    assert r.market_id == "0xabc" and r.estimated_p == 0.4 and r.p_low == 0.3 and r.p_high == 0.5
    body_only = build_research_markdown(_complete(markdown="## Bull Thesis\n\n- a\n\n## Bear Thesis\n\n- b\n",
                                                  market_id="0xabc", estimated_p=0.4, p_low=0.3, p_high=0.5))
    assert parse_deep_researcher(body_only).estimated_p == 0.4


def test_live_prompt_puts_input_before_response_hint():
    from orchestrator.runner import _build_live_prompt
    prompt = _build_live_prompt("deep_researcher", {"output_is_state_machine": True, "live_response_hint": "HINT_X"},
                                {"market_id": "m"})
    assert prompt.index("Input JSON:") < prompt.index("Return ONLY") < prompt.index("HINT_X")
    assert prompt.rstrip().endswith("HINT_X")
