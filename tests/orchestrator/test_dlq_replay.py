"""Tests for DLQ recovery via replay_from_dlq()."""

from __future__ import annotations

import pytest

from obsidian_utils import ObsidianManager
from orchestrator.dead_letter import quarantine_market, replay_from_dlq


@pytest.fixture
def vault(tmp_path):
    return ObsidianManager(vault_base=tmp_path)


def _write_active(vault: ObsidianManager, market_id: str, body: str = "research") -> None:
    (vault._dirs["active"] / f"{market_id}.md").write_text(body, encoding="utf-8")


def _write_filter(vault: ObsidianManager, market_id: str) -> None:
    (vault._dirs["filters"] / f"{market_id}.md").write_text("---\npassed: true\n---\n", encoding="utf-8")


def test_quarantine_records_manifest(vault):
    _write_active(vault, "0xabc", "body")
    _write_filter(vault, "0xabc")

    quarantine_market(vault, "0xabc", "phase3 error", {"error": "bad output"})

    logs = vault.iter_error_logs("0xabc")
    assert len(logs) == 1
    record = vault.read_error_log(logs[0])
    manifest = record["quarantined_artifacts"]
    assert len(manifest) == 2
    origins = {entry["origin_key"] for entry in manifest}
    assert origins == {"active", "filters"}
    assert all(entry["stored_filename"] for entry in manifest)
    assert not vault.active_research_path("0xabc").exists()
    assert not (vault._dirs["filters"] / "0xabc.md").exists()


def test_replay_restores_artifacts_and_clears_log(vault):
    _write_active(vault, "0xabc", "restored-body")
    quarantine_market(vault, "0xabc", "failure", {"x": 1})

    summary = replay_from_dlq(vault, market_ids=["0xabc"])

    assert summary["restored"] == 1
    assert summary["logs_cleared"] == 1
    assert vault.active_research_path("0xabc").exists()
    assert vault.active_research_path("0xabc").read_text(encoding="utf-8") == "restored-body"
    assert vault.iter_error_logs("0xabc") == []


def test_replay_dry_run_does_not_move_or_clear(vault):
    _write_active(vault, "0xabc", "keep-in-dlq")
    quarantine_market(vault, "0xabc", "failure", {})

    summary = replay_from_dlq(vault, market_ids=["0xabc"], dry_run=True)

    assert summary["restored"] == 1
    assert summary["logs_cleared"] == 0
    assert not vault.active_research_path("0xabc").exists()
    assert len(vault.iter_error_logs("0xabc")) == 1


def test_replay_skips_when_destination_exists(vault):
    _write_active(vault, "0xabc", "quarantined")
    quarantine_market(vault, "0xabc", "failure", {})
    _write_active(vault, "0xabc", "live-copy")

    summary = replay_from_dlq(vault, market_ids=["0xabc"])

    assert summary["skipped"] == 1
    assert summary["restored"] == 0
    assert vault.active_research_path("0xabc").read_text(encoding="utf-8") == "live-copy"


def test_replay_filters_by_market_id(vault):
    _write_active(vault, "0xabc", "abc")
    _write_active(vault, "0xdef", "def")
    quarantine_market(vault, "0xabc", "fail abc", {})
    quarantine_market(vault, "0xdef", "fail def", {})

    summary = replay_from_dlq(vault, market_ids=["0xabc"])

    assert vault.active_research_path("0xabc").exists()
    assert not vault.active_research_path("0xdef").exists()
    assert len(vault.iter_error_logs("0xdef")) == 1
    assert summary["markets"]["0xabc"]["restored"] == 1


def test_legacy_replay_without_manifest(vault):
    market_id = "0xlegacy"
    (vault._dirs["errors"] / f"{market_id}.md").write_text("legacy research", encoding="utf-8")
    log_path = vault.write_error_log(market_id, {}, "legacy failure")

    summary = replay_from_dlq(vault, market_ids=[market_id])

    assert summary["restored"] == 1
    assert vault.active_research_path(market_id).read_text(encoding="utf-8") == "legacy research"
    assert not log_path.exists()


def test_replay_all_processes_every_log(vault):
    _write_active(vault, "0xone", "one")
    _write_active(vault, "0xtwo", "two")
    quarantine_market(vault, "0xone", "fail", {})
    quarantine_market(vault, "0xtwo", "fail", {})

    summary = replay_from_dlq(vault, market_ids=None)

    assert summary["restored"] == 2
    assert summary["logs_cleared"] == 2
    assert vault.active_research_path("0xone").exists()
    assert vault.active_research_path("0xtwo").exists()
