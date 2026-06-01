"""Tests for in-place inactive flagging and replay."""

from __future__ import annotations

import pytest

from config.trading_constants import ERROR_LOG_KEY, STATUS_INACTIVE, STATUS_KEY
from obsidian_utils import ObsidianManager
from orchestrator.state import flag_inactive, is_inactive, replay_inactive


@pytest.fixture
def vault(tmp_path):
    return ObsidianManager(vault_base=tmp_path)


def test_flag_inactive_on_filter(vault):
    vault.write_filter_log(
        "0xabc",
        {
            "market_id": "0xabc",
            "passed": True,
            "trigger": "stub",
            "confidence_multiplier": 1.0,
            "details": "ok",
            "error": None,
        },
    )
    flag_inactive(vault, "0xabc", "phase2", "evaluator error", {"x": 1})

    record = vault.read_filter_log("0xabc")
    assert record is not None
    assert is_inactive(record)
    assert record[ERROR_LOG_KEY]["reason"] == "evaluator error"


def test_replay_clears_inactive_flag(vault):
    vault.write_filter_log(
        "0xabc",
        {
            "market_id": "0xabc",
            "passed": True,
            "trigger": "stub",
            "confidence_multiplier": 1.0,
            "details": "ok",
            "error": None,
            STATUS_KEY: STATUS_INACTIVE,
            ERROR_LOG_KEY: {"reason": "fail"},
        },
    )

    summary = replay_inactive(vault, market_ids=["0xabc"])

    assert summary["cleared"] >= 1
    record = vault.read_filter_log("0xabc")
    assert record is not None
    assert not is_inactive(record)
    assert ERROR_LOG_KEY not in record


def test_replay_dry_run_does_not_clear(vault):
    vault.patch_frontmatter(
        "0xabc",
        "filters",
        {STATUS_KEY: STATUS_INACTIVE, ERROR_LOG_KEY: {"reason": "fail"}},
    )
    (vault._dirs["filters"] / "0xabc.md").write_text(
        "---\n"
        "market_id: 0xabc\n"
        "passed: true\n"
        "confidence_multiplier: 1.0\n"
        "details: ok\n"
        f"status: {STATUS_INACTIVE}\n"
        "---\n",
        encoding="utf-8",
    )

    summary = replay_inactive(vault, market_ids=["0xabc"], dry_run=True)

    assert summary["cleared"] >= 1
    record = vault.read_filter_log("0xabc")
    assert is_inactive(record)


def test_replay_filters_by_market_id(vault):
    for mid in ("0xabc", "0xdef"):
        vault.write_filter_log(
            mid,
            {
                "market_id": mid,
                "passed": True,
                "trigger": "stub",
                "confidence_multiplier": 1.0,
                "details": "ok",
                "error": None,
                STATUS_KEY: STATUS_INACTIVE,
                ERROR_LOG_KEY: {"reason": "fail"},
            },
        )

    replay_inactive(vault, market_ids=["0xabc"])

    assert not is_inactive(vault.read_filter_log("0xabc"))
    assert is_inactive(vault.read_filter_log("0xdef"))
