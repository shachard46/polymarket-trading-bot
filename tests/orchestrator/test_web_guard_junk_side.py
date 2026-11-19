"""WEB-GUARD reason counters, junk-payload format retries, comparator follow-up logging, below-edge side."""

from __future__ import annotations

import logging

import pytest

from orchestrator import brave_search, evidence
from orchestrator.parse import junk_payload_reason
from orchestrator.phases import agent_call, p4_execution
from orchestrator.runner import _validate_structured
from orchestrator.schema_validation import AgentSchemaError
from orchestrator.web_filter import NOT_FOUND, filter_research_data

TODAY = "2026-11-19"


def test_filter_names_every_drop_path():
    cites = ["https://a.example/1"]
    _, c = filter_research_data(NOT_FOUND, cites)
    assert c.not_found == 1 and c.kept == 0
    _, c = filter_research_data("- ok | https://a.example/1/ | 2026-11-01\nNOT_FOUND\n- nourl | unknown", cites)
    assert (c.kept, c.dropped_blank, c.dropped_no_url) == (1, 1, 1)
    _, c = filter_research_data("   ", cites)
    assert c.dropped_blank == 1
    assert "not_found=" in c.reason() and "dropped_blank=" in c.reason()


def test_trailing_slash_query_and_www_still_match():
    page = "https://www.a.example/x/?id=1"
    text = "- f | https://a.example/x?id=1 | 2026-11-01"
    _, c = filter_research_data(text, [page])
    assert c.kept == 1


def test_guard_log_says_not_found_when_extractor_found_nothing(monkeypatch, caplog):
    page = brave_search.Page(url="https://a.example/1", title="t", page_age=None, text="x", fetched=True)
    monkeypatch.setattr(brave_search, "search_pages", lambda q: [page])
    runner = lambda role, payload: {"market_id": "m1", "results": [  # noqa: E731
        {"query": "q", "research_data": "NOT_FOUND", "error": None}], "error": None}
    with caplog.at_level(logging.WARNING):
        evidence._brave_one(runner, "m1", "q", TODAY)
    msgs = [r.getMessage() for r in caplog.records if "WEB-GUARD" in r.getMessage()]
    assert msgs and "not_found=1" in msgs[0]


def test_junk_payload_reason():
    assert junk_payload_reason({": ": None, "market_id": "m", "results": [": "], "error": None})
    assert junk_payload_reason({})
    assert junk_payload_reason({"market_id": "m", "results": []}) is None


def test_validate_structured_rejects_junk_keys():
    spec = {"output_model": object()}
    with pytest.raises(AgentSchemaError, match="junk"):
        _validate_structured("web_extractor", spec, {": ": None, "market_id": "m", "results": [], "error": None}, {})


def test_web_extractor_junk_output_is_retried(monkeypatch):
    calls = []

    def runner(role, payload):
        calls.append(payload.get("format_validation_error"))
        if len(calls) == 1:
            raise AgentSchemaError(role, "output", "junk payload: junk keys [': ']")
        return {"market_id": "m1", "results": [{"query": "q", "research_data": "NOT_FOUND", "error": None}], "error": None}

    rows = evidence._agent_rows(runner, "web_extractor", {"market_id": "m1"}, "m1", ["q"])
    assert len(calls) == 2 and calls[1] and rows[0]["research_data"] == "NOT_FOUND" and not rows[0]["error"]


def test_run_structured_agent_retries_schema_failure():
    calls = []

    def runner(role, payload):
        calls.append(payload.get("format_validation_error"))
        if len(calls) == 1:
            raise AgentSchemaError(role, "output", "junk payload")
        return {"researchable": True, "manipulation_risk": "low", "reason": "r", "error": None}

    parsed, err = agent_call.run_structured_agent(runner, "triage", {"market_id": "m"})
    assert err is None and len(calls) == 2 and calls[1]


def test_run_structured_agent_gives_up_after_retries():
    def runner(role, payload):
        raise AgentSchemaError(role, "output", "junk payload")

    parsed, err = agent_call.run_structured_agent(runner, "triage", {"market_id": "m"})
    assert parsed is None and "schema mismatch" in err


QUOTES = {"YES": {"ask": 0.81}, "NO": {"ask": 0.21}}


def test_below_edge_side_follows_edge_sign():
    alloc = {"side": "YES", "price": 0.81, "binding_cap": "min_edge"}
    out = p4_execution._edge_sign_side(alloc, 0.535, 0.80, 0.25, 0.78, QUOTES)
    assert (out["side"], out["price"]) == ("NO", 0.21)
    out = p4_execution._edge_sign_side(alloc, 0.9, 0.5, 0.8, 0.95, QUOTES)
    assert (out["side"], out["price"]) == ("YES", 0.81)
    out = p4_execution._edge_sign_side(alloc, 0.5, 0.5, 0.25, 0.78, QUOTES)  # q inside the range
    assert out["side"] is None and out["price"] is None


def test_non_edge_below_keeps_skill_side_and_live_untouched():
    alloc = {"side": "YES", "price": 0.81, "binding_cap": "min_order"}
    assert p4_execution._edge_sign_side(alloc, 0.3, 0.8, None, None, QUOTES) is alloc
    live = {"side": "YES", "price": 0.3, "binding_cap": "kelly", "allocation_usd": 5.0}
    assert p4_execution._edge_sign_side(live, 0.6, 0.3, None, None, QUOTES) is live


def test_keyword_query_strips_question_form_and_as_of():
    q = "What is Taiwan's Central Election Commission official candidate list for Taichung? (as of 2026-11-19)"
    out = brave_search.keyword_query(q)
    assert out.startswith("Taiwan's Central Election") and "as of" not in out.lower() and "?" not in out
    assert brave_search.keyword_query("As of 2026-11-19, how many seats did KMT win in 2022?") == "seats did KMT win in 2022"
    assert brave_search.keyword_query("?") == "?"  # never empty
    assert len(brave_search.keyword_query("word " * 100).split()) == brave_search.KEYWORD_QUERY_MAX_WORDS


def test_search_pages_sends_keyword_form_to_brave(monkeypatch):
    sent = []
    monkeypatch.setattr(brave_search, "search", lambda q, freshness=None: sent.append(q) or [])
    brave_search.search_pages("What did polls show? (as of 2026-11-19)")
    assert sent == ["did polls show"] or sent == ["polls show"] or "as of" not in sent[0].lower()


def test_not_found_logs_per_page_diagnostics(monkeypatch, caplog):
    pages = [brave_search.Page(url="https://news.example/a", title="t", page_age=None, text="Cookie banner  accept all", fetched=False)]
    monkeypatch.setattr(brave_search, "search_pages", lambda q: pages)
    runner = lambda role, payload: {"market_id": "m1", "results": [  # noqa: E731
        {"query": "q", "research_data": "NOT_FOUND", "error": None}], "error": None}
    with caplog.at_level(logging.WARNING):
        evidence._brave_one(runner, "m1", "q", TODAY)
    diag = [r.getMessage() for r in caplog.records if "WEB-DIAG" in r.getMessage()]
    assert diag and "news.example" in diag[0] and "Cookie banner accept all" in diag[0] and "'fetched': False" in diag[0]
