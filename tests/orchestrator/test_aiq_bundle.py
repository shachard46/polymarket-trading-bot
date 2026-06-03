"""Unit tests for Hub-side parallel A-IQ bundle fetching."""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

import orchestrator.aiq_bundle as aiq_bundle
from orchestrator.config import RESEARCH_DATA_MAX_CHARS


@dataclass
class FakeAiqOutput:
    research_data: str
    error: str | None = None


@pytest.fixture(autouse=True)
def _reset_aiq_cache():
    """Isolate dynamic import cache between tests."""
    aiq_bundle.reset_execute_aiq_query_cache()
    yield
    aiq_bundle.reset_execute_aiq_query_cache()


def test_fetch_research_bundle_empty_queries():
    assert aiq_bundle.fetch_research_bundle([]) == []


def test_fetch_research_bundle_preserves_order(monkeypatch):
    def fake_execute(query: str) -> FakeAiqOutput:
        return FakeAiqOutput(research_data=f"data:{query}")

    monkeypatch.setattr(aiq_bundle, "_load_execute_aiq_query", lambda: fake_execute)

    queries = ["q-alpha", "q-beta", "q-gamma"]
    results = aiq_bundle.fetch_research_bundle(queries)

    assert [r["query"] for r in results] == queries
    assert [r["research_data"] for r in results] == [f"data:{q}" for q in queries]
    assert all(r["error"] is None for r in results)


def test_fetch_research_bundle_embeds_skill_error(monkeypatch):
    def fake_execute(query: str) -> FakeAiqOutput:
        return FakeAiqOutput(research_data="", error="timeout")

    monkeypatch.setattr(aiq_bundle, "_load_execute_aiq_query", lambda: fake_execute)

    results = aiq_bundle.fetch_research_bundle(["slow query"])
    assert len(results) == 1
    assert results[0]["research_data"] == ""
    assert results[0]["error"] == "timeout"


def test_fetch_research_bundle_worker_exception_does_not_crash(monkeypatch):
    def fake_execute(query: str) -> FakeAiqOutput:
        if query == "boom":
            raise RuntimeError("worker blew up")
        return FakeAiqOutput(research_data=f"ok:{query}")

    monkeypatch.setattr(aiq_bundle, "_load_execute_aiq_query", lambda: fake_execute)

    results = aiq_bundle.fetch_research_bundle(["ok", "boom", "also-ok"])
    assert [r["query"] for r in results] == ["ok", "boom", "also-ok"]
    assert results[0]["research_data"] == "ok:ok"
    assert results[0]["error"] is None
    assert results[1]["research_data"] == ""
    assert results[1]["error"] == "worker blew up"
    assert results[2]["research_data"] == "ok:also-ok"
    assert results[2]["error"] is None


def test_fetch_research_bundle_partial_skill_failures(monkeypatch):
    def fake_execute(query: str) -> FakeAiqOutput:
        if query == "bad":
            return FakeAiqOutput(research_data="", error="A-IQ FAILURE")
        return FakeAiqOutput(research_data="content")

    monkeypatch.setattr(aiq_bundle, "_load_execute_aiq_query", lambda: fake_execute)

    results = aiq_bundle.fetch_research_bundle(["good", "bad"])
    assert results[0]["error"] is None
    assert results[0]["research_data"] == "content"
    assert results[1]["error"] == "A-IQ FAILURE"
    assert results[1]["research_data"] == ""


def test_fetch_research_bundle_submits_all_queries_in_parallel(monkeypatch):
    seen: list[str] = []

    def fake_execute(query: str) -> FakeAiqOutput:
        seen.append(query)
        return FakeAiqOutput(research_data=query)

    monkeypatch.setattr(aiq_bundle, "_load_execute_aiq_query", lambda: fake_execute)

    queries = [f"q{i}" for i in range(5)]
    results = aiq_bundle.fetch_research_bundle(queries)
    assert [r["query"] for r in results] == queries
    assert set(seen) == set(queries)


def test_fetch_research_bundle_truncates_long_research_data(monkeypatch):
    long_body = "x" * (RESEARCH_DATA_MAX_CHARS + 500)

    def fake_execute(query: str) -> FakeAiqOutput:
        return FakeAiqOutput(research_data=long_body)

    monkeypatch.setattr(aiq_bundle, "_load_execute_aiq_query", lambda: fake_execute)

    results = aiq_bundle.fetch_research_bundle(["long"])
    data = results[0]["research_data"]
    assert len(data) <= RESEARCH_DATA_MAX_CHARS + len("\n...[truncated]")
    assert data.endswith("...[truncated]")
    assert data.startswith("x")


def test_fetch_research_bundle_respects_aiq_timeout_env(monkeypatch):
    seen: list[str | None] = []

    def fake_execute(query: str) -> FakeAiqOutput:
        seen.append(os.environ.get("AIQ_TIMEOUT_SEC"))
        return FakeAiqOutput(research_data=f"ok:{query}")

    monkeypatch.setattr(aiq_bundle, "_load_execute_aiq_query", lambda: fake_execute)
    monkeypatch.setenv("AIQ_TIMEOUT_SEC", "1200")

    aiq_bundle.fetch_research_bundle(["q1"])
    assert seen == ["1200"]
    assert os.environ.get("AIQ_TIMEOUT_SEC") == "1200"


def test_reset_execute_aiq_query_cache_clears_loader(monkeypatch):
    calls = {"n": 0}

    def fake_loader() -> object:
        calls["n"] += 1
        return lambda _q: FakeAiqOutput(research_data="x")

    monkeypatch.setattr(aiq_bundle, "_load_execute_aiq_query", fake_loader)
    aiq_bundle.reset_execute_aiq_query_cache()
    aiq_bundle.fetch_research_bundle(["a"])
    aiq_bundle.reset_execute_aiq_query_cache()
    aiq_bundle.fetch_research_bundle(["b"])
    assert calls["n"] == 2
