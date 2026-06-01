"""Vault persistence for Phase 3 research bundles."""

from __future__ import annotations

import json

import pytest

from obsidian_utils import ObsidianManager


@pytest.fixture()
def vault(tmp_path):
    return ObsidianManager(vault_base=tmp_path)


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


def test_read_research_bundle_empty_queries_returns_empty_list(vault):
    path = vault._research_bundle_path("0xabc")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"market_id": "0xabc", "fetched_at": "t", "queries": []}),
        encoding="utf-8",
    )
    assert vault.read_research_bundle("0xabc") == []
