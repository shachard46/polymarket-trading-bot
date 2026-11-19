"""Web evidence hallucination guard: filter, citations parsing, request bodies, stats line, stub."""

from __future__ import annotations

import json
import types

import pytest

from orchestrator import direct_runner, evidence
from orchestrator.llm import usage
from orchestrator.llm.base import LLMResponse
from orchestrator.llm.gemini_llm import GeminiLLM
from orchestrator.llm.openrouter_llm import OpenRouterLLM
from orchestrator.llm.anthropic_llm import _urls_of
from orchestrator.runner import spawn_agent
from orchestrator.stats import compute_stats, render_stats
from orchestrator.web_filter import NOT_FOUND, filter_research_data, normalize_url

CITES = ["https://www.example.org/a/", "http://news.com/x?id=1"]


def test_no_url_dropped():
    text = "- a | https://example.org/a | unknown\n- b without source"
    out, c = filter_research_data(text, None, strict=False)
    assert out == "- a | https://example.org/a | unknown"
    assert (c.kept, c.dropped_no_url, c.dropped_unretrieved) == (1, 1, 0)


def test_unretrieved_dropped_and_normalised():
    text = (
        "- a | https://example.org/a | 2026-01-01\n"
        "- b | HTTP://News.com/x?id=1#frag | unknown\n"
        "- c | https://invented.org/z | unknown"
    )
    out, c = filter_research_data(text, CITES)
    assert out.splitlines() == text.splitlines()[:2]
    assert (c.kept, c.dropped_no_url, c.dropped_unretrieved) == (2, 0, 1)


def test_all_dropped_becomes_not_found():
    out, c = filter_research_data("- x | https://invented.org | unknown\nplain", CITES)
    assert out == NOT_FOUND and c.kept == 0 and c.dropped_no_url == 1 and c.dropped_unretrieved == 1


def test_not_found_passthrough():
    out, c = filter_research_data(" NOT_FOUND\n", CITES)
    assert out == NOT_FOUND and (c.kept, c.dropped_no_url, c.dropped_unretrieved) == (0, 0, 0)


def test_citations_none_is_unverifiable():
    out, c = filter_research_data("- a | https://anything.org/q | unknown\n- b no url", None)
    assert out == NOT_FOUND
    assert (c.kept, c.dropped_no_url, c.dropped_unretrieved, c.dropped_unverifiable) == (0, 1, 0, 1)
    # non-strict (no provider call happened, e.g. stub runner): URL rule only
    out, c = filter_research_data("- a | https://anything.org/q | unknown", None, strict=False)
    assert out.startswith("- a") and c.kept == 1
    # empty (not None) citations list means nothing was retrieved
    out, c = filter_research_data("- a | https://anything.org/q | unknown", [])
    assert out == NOT_FOUND and c.dropped_unretrieved == 1


def test_domain_citation_matches_host():
    cites = ["https://vertexaisearch.cloud.google.com/grounding-api-redirect/AAA", "cec.gov.tw"]
    text = "- a | https://db.cec.gov.tw/x | unknown\n- b | https://other.org/y | unknown"
    out, c = filter_research_data(text, cites)
    assert out == "- a | https://db.cec.gov.tw/x | unknown" and c.dropped_unretrieved == 1
    redirect = "- r | https://vertexaisearch.cloud.google.com/grounding-api-redirect/AAA | unknown"
    assert filter_research_data(redirect, cites)[0] == redirect


def test_normalize_url():
    assert normalize_url("https://www.Example.org/a/") == normalize_url("http://example.org/a")


# ---- citations per provider -------------------------------------------------------------------


def test_openrouter_citations_and_body():
    llm = OpenRouterLLM("vendor/m")
    msg = {"content": "x", "annotations": [
        {"type": "url_citation", "url_citation": {"url": "https://a.org/1", "title": "t"}},
        {"type": "url_citation", "url_citation": {"url": "https://a.org/1"}},
        {"type": "other"},
    ]}
    r = llm._parse({"choices": [{"message": msg}]})
    assert r.citations == ["https://a.org/1"]
    assert llm._parse({"choices": [{"message": {"content": "x"}}]}).citations is None
    assert "temperature" not in llm._body("s", "u", False, False)
    llm.temperature, llm.reasoning = 0.0, False
    body = llm._body("s", "u", False, False)
    assert body["temperature"] == 0.0 and body["reasoning"] == {"enabled": False}
    llm.reasoning = True
    assert "reasoning" not in llm._body("s", "u", False, False)


def test_gemini_citations_and_body():
    llm = GeminiLLM("gemini-3.6-flash")
    cand = {"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP",
            "groundingMetadata": {"groundingChunks": [{"web": {"uri": "https://g.org/1"}}, {"web": {"uri": "https://g.org/1"}}, {}]}}
    assert llm._parse({"candidates": [cand]}).citations == ["https://g.org/1"]
    cand["groundingMetadata"]["groundingChunks"] = [{"web": {"uri": "https://r/1", "title": "Cec.gov.tw"}}, {"web": {"title": "Some Title"}}]
    assert llm._parse({"candidates": [cand]}).citations == ["https://r/1", "cec.gov.tw"]
    cand.pop("groundingMetadata")
    assert llm._parse({"candidates": [cand]}).citations is None
    llm.temperature, llm.reasoning = 0.0, False
    cfg = llm._body("s", "u", False, False)["generationConfig"]
    assert cfg["temperature"] == 0.0 and cfg["thinkingConfig"] == {"thinkingLevel": "minimal"}
    llm4 = GeminiLLM("gemini-4-hydrogen")
    llm4.reasoning = False
    assert llm4._body("s", "u", False, False)["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}


def test_anthropic_urls_extraction():
    ns = types.SimpleNamespace
    msg = ns(content=[
        ns(type="web_search_tool_result", content=[ns(url="https://s.org/1"), ns(url="https://s.org/2")], citations=None),
        ns(type="text", text="x", citations=[ns(url="https://s.org/2"), ns(url="https://s.org/3")]),
    ])
    assert _urls_of([msg]) == ["https://s.org/1", "https://s.org/2", "https://s.org/3"]


def test_anthropic_temperature_in_request(monkeypatch):
    from orchestrator.llm import anthropic_llm

    seen = {}

    class Msgs:
        def create(self, **kw):
            seen.update(kw)
            return types.SimpleNamespace(content=[], stop_reason="end_turn", usage=None)

    fake = types.SimpleNamespace(Anthropic=lambda **k: types.SimpleNamespace(messages=Msgs()))
    monkeypatch.setattr(anthropic_llm, "_import_anthropic", lambda: fake)
    llm = anthropic_llm.AnthropicLLM("m")
    llm.temperature = 0.0
    r = llm._send("s", "u", False, True)
    assert seen["temperature"] == 0.0 and r.citations == []
    assert llm._send("s", "u", False, False).citations is None


# ---- direct_runner contextvar / routing ----------------------------------------------------------


def test_last_citations_is_last_call(monkeypatch):
    import agents_blueprint

    seq = iter([["https://a.org"], ["https://b.org"]])

    class Fake:
        def complete(self, *a, **k):
            return LLMResponse("t", "p", "mod", citations=next(seq))

    monkeypatch.setattr(direct_runner.routing, "llm_for", lambda role: Fake())
    spec = agents_blueprint.AGENTS["web_searcher"]
    pl = {"market_id": "m", "queries": ["q"], "today": "2026-01-01"}
    assert direct_runner.last_citations() is None
    direct_runner.run_direct("web_searcher", spec, pl)
    direct_runner.run_direct("web_searcher", spec, pl)
    assert direct_runner.last_citations() == ["https://b.org"] and direct_runner.last_model() == "mod"


def test_routing_passes_temperature_reasoning(tmp_path, monkeypatch):
    from orchestrator.llm import routing

    p = tmp_path / "models.yaml"
    p.write_text(
        "default: {provider: openrouter, model: m}\n"
        "roles:\n  web_searcher: {provider: openrouter, model: m, temperature: 0, reasoning: false}\n"
    )
    monkeypatch.setenv("LLM_MODELS_CONFIG", str(p))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    routing._instances.clear()
    llm = routing.llm_for("web_searcher")
    assert llm.temperature == 0 and llm.reasoning is False
    assert routing.llm_for("triage").temperature is None and routing.llm_for("triage").reasoning is None
    routing._instances.clear()


def test_shipped_models_yaml_web_searcher(monkeypatch):
    from orchestrator.llm import routing

    monkeypatch.setenv("LLM_MODELS_CONFIG", str(routing.DEFAULT_MODELS_PATH))
    spec = routing.load_config().roles["web_searcher"]
    assert (spec.temperature, spec.reasoning, spec.web_max_uses) == (0, False, 5)


# ---- evidence path, stats, stub -------------------------------------------------------------------


def _runner(text):
    def run(role, payload):
        return {"results": [{"query": q, "research_data": text, "error": None} for q in payload["queries"]]}
    return run


def test_fetch_web_rows_filters_and_records(monkeypatch, tmp_path):
    monkeypatch.setenv("PMBOT_VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(direct_runner, "last_citations", lambda: ["https://ok.org/p"])
    monkeypatch.setattr(direct_runner, "last_model", lambda: "vendor/m")
    text = "- a | https://ok.org/p | unknown\n- b | https://bad.org | unknown\n- c"
    rows = evidence.fetch_web_rows(_runner(text), "m1", ["q"], "2026-01-01")
    assert rows[0]["research_data"] == "- a | https://ok.org/p | unknown"
    (r,) = usage.iter_web_filter(tmp_path)
    assert (r["model"], r["kept"], r["dropped_no_url"], r["dropped_unretrieved"]) == ("vendor/m", 1, 1, 1)
    assert r["dropped_unverifiable"] == 0


def test_fetch_web_rows_no_citations_on_web_call_dropped(monkeypatch, tmp_path):
    monkeypatch.setenv("PMBOT_VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(direct_runner, "last_citations", lambda: None)
    monkeypatch.setattr(direct_runner, "last_model", lambda: "vendor/m")
    rows = evidence.fetch_web_rows(_runner("- a | https://invented.org | unknown"), "m1", ["q"], "2026-01-01")
    assert rows[0]["research_data"] == NOT_FOUND
    rs = usage.iter_web_filter(tmp_path)
    assert len(rs) == 2  # no usable row -> one retry
    assert all((r["kept"], r["dropped_unverifiable"]) == (0, 1) for r in rs)


def test_stale_citations_reset_before_call(monkeypatch, tmp_path):
    monkeypatch.setenv("PMBOT_VAULT_PATH", str(tmp_path))
    direct_runner._last_call.set((["https://old.org"], "old"))
    rows = evidence.fetch_web_rows(_runner("- a | https://x.org | unknown"), "m", ["q"], "d")
    assert rows[0]["research_data"].startswith("- a")  # custom runner: citations unknown (None)


def test_stats_web_filter_line():
    wf = [
        {"model": "m1", "kept": 6, "dropped_no_url": 1, "dropped_unretrieved": 1, "dropped_unverifiable": 2},
        {"model": "m1", "kept": 2, "dropped_no_url": 0, "dropped_unretrieved": 0},
        {"model": "m2", "kept": 0, "dropped_no_url": 2, "dropped_unretrieved": 0},
    ]
    md = render_stats(compute_stats([], [], wf))
    assert "## Web evidence filter" in md
    assert "Total: kept 8, dropped (no url) 3, dropped (unretrieved url) 1, dropped (no citations) 2; drop rate" in md
    assert "| m1 | 8 | 1 | 1 | 2 |" in md and "| m2 | 0 | 2 | 0 | 0 |" in md
    assert "No web evidence filter rows yet." in render_stats(compute_stats([], []))


def test_stub_pipeline_still_yields_web_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("PMBOT_VAULT_PATH", str(tmp_path))
    rows = evidence.fetch_web_rows(spawn_agent, "m", ["q1", "q2"], "2026-10-22")
    assert all("stub web finding" in r["research_data"] for r in rows) and evidence.rows_usable(rows)


# ---- wrong-shaped answers, retry, usable rows ----------------------------------------------------


def test_rows_usable_ignores_not_found_and_errors():
    assert not evidence.rows_usable([{"research_data": "NOT_FOUND", "error": None}])
    assert not evidence.rows_usable([{"research_data": "- a | https://x.org | unknown", "error": "boom"}])
    assert evidence.rows_usable([{"research_data": "- a | https://x.org | unknown", "error": None}])


def test_salvage_top_level_research_data_single_query():
    raw = {"error": None, "market_id": "m", "query": "something else", "research_data": "- a | https://x.org | unknown"}
    rows = evidence.fetch_web_rows(lambda role, pl: raw, "m", ["q1"], "2026-01-01")
    assert rows[0]["query"] == "q1" and rows[0]["research_data"].startswith("- a")


def test_salvage_multi_query_matches_by_query_or_text():
    qs = ["alpha query", "beta query"]
    raw = {"query": "Beta Query", "research_data": "- b | https://x.org | unknown"}
    rows = evidence._fetch_web_rows_once(lambda role, pl: raw, "m", qs, "2026-01-01")
    assert rows[0]["error"] == "no result for query" and rows[1]["research_data"].startswith("- b")
    raw = {"research_data": "re alpha query: - a | https://x.org | unknown"}
    rows = evidence._fetch_web_rows_once(lambda role, pl: raw, "m", qs, "2026-01-01")
    assert rows[0]["research_data"].startswith("re alpha") and rows[1]["error"] == "no result for query"


def test_web_searcher_retried_once_when_nothing_usable():
    answers = iter([
        {"error": "x", "research_data": ""},
        {"results": [{"query": "q1", "research_data": "- a | https://x.org | unknown", "error": None}]},
    ])
    calls = []

    def runner(role, pl):
        calls.append(role)
        return next(answers)

    rows = evidence.fetch_web_rows(runner, "m", ["q1"], "2026-01-01")
    assert len(calls) == 2 and evidence.rows_usable(rows)
    # usable first time: no retry
    calls.clear()
    ok = {"results": [{"query": "q1", "research_data": "- a | https://x.org | unknown", "error": None}]}
    evidence.fetch_web_rows(lambda r, p: (calls.append(r), ok)[1], "m", ["q1"], "2026-01-01")
    assert len(calls) == 1
