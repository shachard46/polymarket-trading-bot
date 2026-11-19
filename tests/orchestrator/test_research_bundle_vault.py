"""Vault persistence for Phase 3 research bundles."""

from __future__ import annotations

import json
from typing import Any

from orchestrator import phases
from orchestrator.phases import p3_evidence
from tests.orchestrator.test_phase3_helpers import (
    STUB_MARKET_DATA,
    briefer_ok,
    deep_researcher_complete,
)


def test_read_research_bundle_missing_returns_none(vault):
    assert vault.read_research_bundle("0xabc") is None


def test_write_and_read_research_bundle(vault):
    entries = [
        {"query": "q1", "research_data": "data1", "error": None},
        {"query": "q2", "research_data": "data2", "error": None},
    ]
    path = vault.write_research_bundle("0xabc", entries)
    assert path.exists()
    loaded = vault.read_research_bundle("0xabc")
    assert loaded is not None
    assert len(loaded) == 2
    assert loaded[0]["query"] == "q1"


def test_write_research_bundle_merges_and_dedupes(vault):
    vault.write_research_bundle(
        "0xabc",
        [{"query": "q1", "research_data": "a", "error": None}],
    )
    vault.write_research_bundle(
        "0xabc",
        [
            {"query": "q1", "research_data": "dup", "error": None},
            {"query": "q2", "research_data": "b", "error": None},
        ],
    )
    loaded = vault.read_research_bundle("0xabc")
    assert loaded is not None
    assert [e["query"] for e in loaded] == ["q1", "q2"]
    assert loaded[0]["research_data"] == "a"


def test_write_research_bundle_retry_after_error_replaces_entry(vault):
    vault.write_research_bundle(
        "0xabc",
        [{"query": "q1", "research_data": "", "error": "timeout"}],
    )
    vault.write_research_bundle(
        "0xabc",
        [{"query": "q1", "research_data": "recovered", "error": None}],
    )
    loaded = vault.read_research_bundle("0xabc")
    assert loaded is not None
    assert len(loaded) == 1
    assert loaded[0]["research_data"] == "recovered"
    assert loaded[0]["error"] is None


def test_write_research_bundle_does_not_clobber_success_with_error(vault):
    vault.write_research_bundle(
        "0xabc",
        [{"query": "q1", "research_data": "good", "error": None}],
    )
    vault.write_research_bundle(
        "0xabc",
        [{"query": "q1", "research_data": "", "error": "still failing"}],
    )
    loaded = vault.read_research_bundle("0xabc")
    assert loaded is not None
    assert loaded[0]["research_data"] == "good"


def test_read_research_bundle_empty_queries_returns_empty_list(vault):
    path = vault._research_bundle_path("0xabc")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"market_id": "0xabc", "fetched_at": "t", "queries": []}),
        encoding="utf-8",
    )
    assert vault.read_research_bundle("0xabc") == []


def _row(market_id: str) -> dict[str, Any]:
    return {
        "market_id": market_id,
        "market_title": "T",
        "market_description": "",
        "market_data": dict(STUB_MARKET_DATA),
    }


def test_poisoned_bundle_is_replanned_and_refetched(vault, monkeypatch):
    vault.write_research_bundle("m-p", [{"query": "q0", "research_data": "", "error": "timeout"}])
    fetched: list[list[str]] = []

    def fetch(qs):
        fetched.append(list(qs))
        return [{"query": q, "research_data": "real", "error": None} for q in qs]

    monkeypatch.setattr(p3_evidence, "fetch_research_bundle", fetch)
    roles: list[str] = []

    def runner(role, payload):
        roles.append(role)
        return briefer_ok(payload) if role == "briefer" else deep_researcher_complete(payload)

    out = phases._research_market(vault, runner, _row("m-p"), "d")
    assert out is not None and "briefer" in roles and fetched
    assert any(e["research_data"] == "real" for e in vault.read_research_bundle("m-p"))


def test_usable_bundle_is_reused_without_planning(vault, monkeypatch):
    vault.write_research_bundle("m-u", [{"query": "q0", "research_data": "ok", "error": None}])

    def runner(role, payload):
        assert role == "deep_researcher"
        assert "q" not in payload and "yes_price" not in payload
        return deep_researcher_complete(payload)

    assert phases._research_market(vault, runner, _row("m-u"), "d")


def test_refresh_always_plans_and_replaces_stale_rows(vault, monkeypatch):
    vault.write_research_bundle("m-e", [{"query": "stale q", "research_data": "STALE", "error": None}])
    monkeypatch.setattr(
        p3_evidence,
        "fetch_research_bundle",
        lambda qs: [{"query": q, "research_data": "FRESH", "error": None} for q in qs],
    )
    roles: list[str] = []

    def runner(role, payload):
        roles.append(role)
        return briefer_ok(payload) if role == "briefer" else deep_researcher_complete(payload)

    phases._research_market(vault, runner, _row("m-e"), "d", refresh=True)
    assert "briefer" in roles
    data = [e["research_data"] for e in vault.read_research_bundle("m-e")]
    assert data and set(data) == {"FRESH"}
