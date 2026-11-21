"""Evidence quality: age labels (A1), Brave backend (A2), gemini one-query-per-call (A3). No network."""

from __future__ import annotations

import logging
import threading

import pytest

from config.trading_constants import BRAVE_RESULTS_PER_QUERY
from orchestrator import brave_search, config, evidence
from orchestrator.llm import usage
from orchestrator.web_filter import NOT_FOUND

TODAY = "2026-11-19"


# ---- A1 labels ------------------------------------------------------------------------------------


def test_label_bundle_ages_stale_undated_and_ids_run_across_rows():
    bundle = [
        {"query": "q1", "research_data": "- fresh | https://a.org | 2026-11-17\n- old | https://b.org | 2026-05-01 (stale)\n"
                                         "- nodate | https://c.org | unknown\nNOT_FOUND-ish line", "error": None},
        {"query": "q2", "research_data": "Plain A-IQ prose line, no date", "error": None},
        {"query": "q3", "research_data": "NOT_FOUND", "error": None},
        {"query": "q4", "research_data": "- x | https://d.org | 2026-11-19", "error": "boom"},
        {"query": "q5", "research_data": "- future | https://e.org | 2027-01-01", "error": None},
    ]
    out = evidence.label_bundle(bundle, TODAY)
    l1 = out[0]["research_data"].splitlines()
    assert l1[0].startswith("[E1 | 2d] - fresh")
    assert l1[1].startswith("[E2 | 202d | STALE] - old")
    assert l1[2].startswith("[E3 | undated] - nodate")
    assert out[1]["research_data"] == "[E4 | undated] Plain A-IQ prose line, no date"
    assert out[2]["research_data"] == "NOT_FOUND" and out[3]["research_data"] == bundle[3]["research_data"]
    assert out[4]["research_data"].startswith("[E5 | 0d] ")
    assert bundle[0]["research_data"].startswith("- fresh")  # stored data is not mutated


def test_stale_boundary_and_bad_today():
    row = [{"query": "q", "research_data": "- a | https://a.org | 2026-05-23", "error": None}]  # exactly 180d
    assert evidence.label_bundle(row, TODAY)[0]["research_data"].startswith("[E1 | 180d] ")
    assert evidence.label_bundle(row, "not-a-date")[0]["research_data"].startswith("[E1 | undated] ")


def test_dr_payload_labels_the_bundle_only_at_render():
    from orchestrator.phases.common import _dr_payload

    bundle = [{"query": "q", "research_data": "- a | https://a.org | 2026-11-18", "error": None}]
    row = {"market_id": "m", "market_title": "t", "market_data": {}}
    p = _dr_payload(row, "d", bundle, mode="full", today=TODAY)
    assert p["research_bundle"][0]["research_data"].startswith("[E1 | 1d] ")
    assert bundle[0]["research_data"].startswith("- a")


# ---- A3 gemini: one query per call ----------------------------------------------------------------


def test_gemini_one_query_per_call_in_parallel_with_per_query_retry(monkeypatch):
    calls, lock = [], threading.Lock()

    def runner(role, payload):
        with lock:
            calls.append((role, list(payload["queries"])))
            n = sum(1 for _, q in calls if q == payload["queries"])
        q = payload["queries"][0]
        if q == "q2" and n == 1:
            return {"error": "x", "research_data": ""}  # first try for q2 unusable -> retried alone
        return {"results": [{"query": q, "research_data": f"- {q} | https://x.org/{q} | unknown", "error": None}]}

    rows = evidence.fetch_web_rows(runner, "m", ["q1", "q2", "q3"], TODAY)
    assert [r["query"] for r in rows] == ["q1", "q2", "q3"] and evidence.rows_usable(rows)
    assert all(len(q) == 1 for _, q in calls)
    assert sorted(q[0] for _, q in calls) == ["q1", "q2", "q2", "q3"]


def test_pages_per_query_logged(caplog):
    runner = lambda role, pl: {"results": [{"query": pl["queries"][0], "research_data": "- a | https://x.org/1 | unknown\n- b | https://x.org/2 | unknown", "error": None}]}  # noqa: E731
    with caplog.at_level(logging.INFO, logger="orchestrator.evidence"):
        evidence.fetch_web_rows(runner, "m", ["q1", "q2"], TODAY)
    assert any("[WEB-PAGES]" in r.message and "pages_per_query=[2, 2]" in r.message for r in caplog.records)


# ---- A2 Brave -------------------------------------------------------------------------------------


class FakeResp:
    def __init__(self, status=200, body=None, text="", ctype="text/html; charset=utf-8", headers=None):
        self.status_code, self._body, self.encoding = status, body, "utf-8"
        self._raw = text.encode()
        self.headers = {"Content-Type": ctype, **(headers or {})}

    def json(self):
        return self._body

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._raw), chunk_size):
            yield self._raw[i:i + chunk_size]

    def close(self):
        pass


HTML = "<html><head><title>t</title><style>x{}</style></head><body><nav>menu</nav><script>evil()</script><p>Pollsters  said <b>42%</b>.</p><div>Second line</div></body></html>"


def test_html_to_text_strips_scripts_and_collapses():
    assert brave_search.html_to_text(HTML) == "Pollsters said 42% .\nSecond line".replace("% .", "%.")


def test_search_headers_params_and_429_backoff(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    seen, sleeps = [], []
    answers = iter([FakeResp(429, headers={"Retry-After": "2"}), FakeResp(200, {"web": {"results": [{"url": "https://a.org"}, {"title": "no url"}]}})])

    def fake_get(url, **kw):
        seen.append((url, kw))
        return next(answers)

    monkeypatch.setattr(brave_search.requests, "get", fake_get)
    monkeypatch.setattr(brave_search, "_sleep", sleeps.append)
    res = brave_search.search("hello world")
    assert [r["url"] for r in res] == ["https://a.org"] and sleeps == [2.0]
    url, kw = seen[0]
    assert url == brave_search.BRAVE_URL and kw["params"]["q"] == "hello world" and kw["params"]["count"] == BRAVE_RESULTS_PER_QUERY
    assert kw["headers"]["X-Subscription-Token"] == "k" and kw["headers"]["Accept"] == "application/json"


def test_search_errors(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    with pytest.raises(brave_search.BraveError):
        brave_search.search("q")
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    monkeypatch.setattr(brave_search, "_sleep", lambda s: None)
    monkeypatch.setattr(brave_search.requests, "get", lambda *a, **k: FakeResp(429))
    with pytest.raises(brave_search.BraveError, match="429"):
        brave_search.search("q")
    monkeypatch.setattr(brave_search.requests, "get", lambda *a, **k: FakeResp(500))
    with pytest.raises(brave_search.BraveError, match="500"):
        brave_search.search("q")


def test_fetch_page_text_skips_pdf_private_and_caps(monkeypatch):
    monkeypatch.setattr(brave_search.requests, "get", lambda *a, **k: FakeResp(text="%PDF", ctype="application/pdf"))
    assert brave_search.fetch_page_text("https://a.org/x.pdf") is None
    assert brave_search.fetch_page_text("http://127.0.0.1/x") is None
    assert brave_search.fetch_page_text("http://localhost/x") is None
    assert brave_search.fetch_page_text("file:///etc/passwd") is None
    monkeypatch.setattr(brave_search, "BRAVE_PAGE_MAX_BYTES", 40)
    monkeypatch.setattr(brave_search.requests, "get", lambda *a, **k: FakeResp(text="<p>" + "a" * 500 + "</p>"))
    assert len(brave_search.fetch_page_text("https://a.org/x")) <= 40


def _fake_web(monkeypatch, results):
    monkeypatch.setenv("BRAVE_API_KEY", "k")

    def fake_get(url, **kw):
        if url == brave_search.BRAVE_URL:
            return FakeResp(200, {"web": {"results": results}})
        return FakeResp(text=HTML)

    monkeypatch.setattr(brave_search.requests, "get", fake_get)
    monkeypatch.setattr(config, "web_backend", lambda: config.WEB_BACKEND_BRAVE)


RESULTS = [
    {"url": "https://a.org/1", "title": "A", "description": "snip", "extra_snippets": ["more"], "page_age": "2026-11-10T08:00:00"},
    {"url": "https://b.org/2", "title": "B", "description": "d2"},
]


def test_brave_backend_extractor_payload_guard_dates_and_sanitize(monkeypatch, tmp_path):
    monkeypatch.setenv("PMBOT_VAULT_PATH", str(tmp_path))
    _fake_web(monkeypatch, RESULTS)
    payloads = []

    def runner(role, payload):
        payloads.append((role, payload))
        return {"results": [{"query": payload["query"], "research_data": (
            "- poll 42% | https://a.org/1 | unknown\n"
            "- invented | https://invented.org/z | 2026-01-01\n"
            "- Polymarket odds 70% | https://b.org/2 | 2026-11-01"), "error": None}]}

    rows = evidence.fetch_web_rows(runner, "m", ["q1"], TODAY)
    role, pl = payloads[0]
    assert role == "web_extractor" and pl["query"] == "q1" and pl["today"] == TODAY
    assert [p["url"] for p in pl["pages"]] == ["https://a.org/1", "https://b.org/2"]
    assert pl["pages"][0]["page_age"] == "2026-11-10" and "Pollsters said 42%" in pl["pages"][0]["text"]
    assert "snip" in pl["pages"][0]["text"] and "evil()" not in pl["pages"][0]["text"]
    lines = rows[0]["research_data"].splitlines()
    assert lines[0] == "- poll 42% | https://a.org/1 | 2026-11-10"  # unknown date -> page_age
    assert evidence.REDACTED in lines[1] and len(lines) == 2  # invented URL dropped; odds line redacted
    (r,) = usage.iter_web_filter(tmp_path)
    assert (r["model"], r["kept"], r["dropped_unretrieved"]) == (evidence.BRAVE_MODEL, 2, 1)


def test_brave_fetch_pages_limit(monkeypatch):
    _fake_web(monkeypatch, [{"url": f"https://s{i}.org/", "description": "d"} for i in range(8)])
    monkeypatch.setattr(brave_search, "BRAVE_FETCH_PAGES", 3)
    assert len(brave_search.search_pages("q", 3)) == 3


def test_brave_no_results_is_not_found_without_extractor(monkeypatch):
    _fake_web(monkeypatch, [])
    rows = evidence.fetch_web_rows(lambda r, p: pytest.fail("extractor must not run"), "m", ["q"], TODAY)
    assert rows[0]["research_data"] == NOT_FOUND and rows[0]["error"] is None


def test_brave_failure_falls_back_to_gemini_per_query(monkeypatch, caplog):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    monkeypatch.setattr(config, "web_backend", lambda: config.WEB_BACKEND_BRAVE)
    monkeypatch.setattr(brave_search.requests, "get", lambda *a, **k: FakeResp(500))
    roles = []

    def runner(role, payload):
        roles.append(role)
        return {"results": [{"query": payload["queries"][0], "research_data": "- a | https://x.org | unknown", "error": None}]}

    with caplog.at_level(logging.WARNING, logger="orchestrator.evidence"):
        rows = evidence.fetch_web_rows(runner, "m", ["q1", "q2"], TODAY)
    assert roles == ["web_searcher", "web_searcher"] and evidence.rows_usable(rows)
    assert any("[WEB-FALLBACK]" in r.message for r in caplog.records)


def test_brave_extractor_error_falls_back(monkeypatch):
    _fake_web(monkeypatch, RESULTS)

    def runner(role, payload):
        if role == "web_extractor":
            raise RuntimeError("deepseek down")
        return {"results": [{"query": payload["queries"][0], "research_data": "- a | https://x.org | unknown", "error": None}]}

    rows = evidence.fetch_web_rows(runner, "m", ["q1"], TODAY)
    assert evidence.rows_usable(rows)


# ---- backend selection ----------------------------------------------------------------------------


def test_web_backend_selection(monkeypatch):
    assert config.web_backend() == "gemini"  # no key
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    assert config.web_backend() == "gemini"  # stub mode never goes to the network
    monkeypatch.setenv("PMBOT_MODE", "live")
    assert config.web_backend() == "brave"
    monkeypatch.setenv("PMBOT_WEB_BACKEND", "gemini")
    assert config.web_backend() == "gemini"
    monkeypatch.setenv("PMBOT_WEB_BACKEND", "brave")
    monkeypatch.delenv("BRAVE_API_KEY")
    assert config.web_backend() == "gemini"  # explicit brave without a key falls back
    monkeypatch.setenv("PMBOT_WEB_BACKEND", "bing")
    with pytest.raises(ValueError):
        config.web_backend()


def test_web_extractor_role_is_registered_without_web_tools():
    import agents_blueprint
    from orchestrator import direct_runner
    from orchestrator.runner import spawn_agent

    spec = agents_blueprint.AGENTS["web_extractor"]
    assert direct_runner._web_for(spec) is False
    out = spawn_agent("web_extractor", {"market_id": "m", "query": "q", "today": TODAY,
                                         "pages": [{"url": "https://a.org", "title": "", "page_age": None, "text": "t"}]})
    assert "https://a.org" in out["results"][0]["research_data"]
