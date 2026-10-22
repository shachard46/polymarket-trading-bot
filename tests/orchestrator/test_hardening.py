"""Regression tests for the vault / parsing / CLI hardening pass."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from obsidian_utils import ObsidianManager, VaultWriteError, _atomic_write_text
from orchestrator import openclaw_cli
from orchestrator.agent_outputs import parse_deep_researcher_json
from orchestrator.directives import extract_filter_directives
from orchestrator.parse import (
    AgentOutputParseError,
    coerce_deep_researcher_state_machine,
    parse_agent_json_or_yaml,
)
from orchestrator.research import split_yaml_frontmatter_markdown
from orchestrator.scraper import select_q
from orchestrator.schema_validation import AgentSchemaError, build_model, validate_payload
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def _reset_cli_cache():
    openclaw_cli.reset_agent_cli_capability_cache()
    yield
    openclaw_cli.reset_agent_cli_capability_cache()


def test_yaml_error_becomes_value_error():
    with pytest.raises(ValueError):
        split_yaml_frontmatter_markdown("---\nkey: [unclosed\n---\nbody")


def test_atomic_write_leaves_no_partial_file(tmp_path, monkeypatch):
    target = tmp_path / "f.txt"
    target.write_text("old", encoding="utf-8")

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        _atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["f.txt"]


def test_hex_market_id_roundtrips_as_string(tmp_path):
    vault = ObsidianManager(tmp_path)
    mid = "0x1f"
    vault.write_research_report(mid, {"market_id": mid, "estimated_p": 0.4}, "body")
    fm = vault.read_market_record(mid, "active")
    assert fm["market_id"] == "0x1f"
    vault.patch_frontmatter(mid, "active", {"status": "x"})
    assert vault.read_market_record(mid, "active")["market_id"] == "0x1f"
    # Numeric-looking ids stay strings even if YAML reads an int.
    fm, _ = split_yaml_frontmatter_markdown("---\nmarket_id: 123\n---\nb")
    assert fm["market_id"] == "123"


def test_unsafe_market_id_rejected(tmp_path):
    vault = ObsidianManager(tmp_path)
    with pytest.raises(ValueError):
        vault.write_trade_log("../evil", {"market_id": "../evil", "allocation_usd": 1, "executed": False})


def test_move_file_refuses_overwrite(tmp_path):
    vault = ObsidianManager(tmp_path)
    vault.write_research_report("m1", {"market_id": "m1", "estimated_p": 0.4}, "b")
    vault.write_file("Vault/04_Post_Mortems/m1.md", "existing")
    with pytest.raises(FileExistsError):
        vault.move_file("m1", "active", "post_mortem")


def test_post_mortem_idempotent_and_replaces(tmp_path):
    vault = ObsidianManager(tmp_path)
    vault.write_file("Vault/04_Post_Mortems/m1.md", "---\nmarket_id: m1\n---\n\n## Bull Thesis\n\nx\n\n## Post-Mortem\n")
    vault.append_post_mortem("m1", {"market_id": "m1", "post_mortem_analysis": "first"})
    vault.append_post_mortem("m1", {"market_id": "m1", "post_mortem_analysis": "first"})
    vault.append_post_mortem("m1", {"market_id": "m1", "post_mortem_analysis": "second"})
    text = vault.read_post_mortem("m1")
    assert text.count("## Post-Mortem") == 1
    assert "first" not in text and "second" in text


def test_corrupt_bundle_backed_up(tmp_path):
    vault = ObsidianManager(tmp_path)
    path = vault._research_bundle_path("m1")
    path.write_text("{not json", encoding="utf-8")
    vault.write_research_bundle("m1", [{"query": "q", "research_data": "d"}])
    assert path.with_name(path.name + ".corrupt").read_text(encoding="utf-8") == "{not json"
    assert vault.read_research_bundle("m1")


def test_directives_backup_and_filters_required(tmp_path):
    vault = ObsidianManager(tmp_path)
    vault.cold_start_protocol()
    seed = vault.read_directives()
    bad = seed.replace("```yaml", "```text")
    with pytest.raises(VaultWriteError):
        vault.write_directives({"new_directives_markdown": bad, "rationale": "r"})
    vault.write_directives({"new_directives_markdown": seed + "\n", "rationale": "r"})
    bak = tmp_path / "Vault" / "00_System" / "active_directives.md.bak"
    if not bak.exists():
        bak = next(tmp_path.rglob("active_directives.md.bak"))
    assert bak.read_text(encoding="utf-8") == seed


def test_strict_bool_rejected_and_nan_rejected():
    model = build_model("T", {"passed": "boolean", "x": "float"})
    with pytest.raises(AgentSchemaError):
        validate_payload("r", "output", model, {"passed": "false", "x": 1.0})
    with pytest.raises(AgentSchemaError):
        validate_payload("r", "output", model, {"passed": True, "x": float("nan")})
    validate_payload("r", "output", model, {"passed": False, "x": 1})


def _fake_cli(monkeypatch, runner):
    monkeypatch.setattr(openclaw_cli.shutil, "which", lambda b: "/usr/bin/openclaw")
    monkeypatch.setattr(openclaw_cli.subprocess, "run", runner)
    monkeypatch.setattr(openclaw_cli.time, "sleep", lambda s: None)


def test_executioner_single_attempt(monkeypatch):
    calls = []

    def run(cmd, **kw):
        if cmd[1:3] == ["agent", "--help"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    _fake_cli(monkeypatch, run)
    with pytest.raises(openclaw_cli.OpenClawCLIError):
        openclaw_cli.run_agent("executioner", "m", session_key="k", timeout=5)
    assert len(calls) == 1
    calls.clear()
    with pytest.raises(openclaw_cli.OpenClawCLIError):
        openclaw_cli.run_agent("evaluator", "m", session_key="k", timeout=5, retry=False)
    assert len(calls) == 1


def test_oserror_wrapped_and_help_timeout_not_cached(monkeypatch):
    def run(cmd, **kw):
        raise OSError(7, "Argument list too long")

    _fake_cli(monkeypatch, run)
    with pytest.raises(openclaw_cli.OpenClawCLIError):
        openclaw_cli.run_agent("evaluator", "m", session_key="k", timeout=5, retry=False)
    ok, detail = openclaw_cli.gateway_reachable()
    assert ok is False and "Argument list too long" in detail

    def timeout_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 10)

    _fake_cli(monkeypatch, timeout_run)
    with pytest.raises(openclaw_cli.OpenClawCLIError, match="--help probe timed out"):
        openclaw_cli.run_agent("evaluator", "m", session_key="k", timeout=5, retry=False)
    assert openclaw_cli._SESSION_KEY_HELP_FLAG is None


def test_stdout_with_prefix_noise_recovered(monkeypatch):
    def run(cmd, **kw):
        if cmd[1:3] == ["agent", "--help"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="--session-key", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout='warn: x\n{"a": 1}\n', stderr="")

    _fake_cli(monkeypatch, run)
    assert openclaw_cli.run_agent("evaluator", "m", session_key="k", timeout=5) == {"a": 1}


def test_extract_agent_text_prefers_last_payload_over_summary():
    summary = "I reasoned about it. Draft: " + '{"status": "draft"}' + " " + "pad " * 500
    out = openclaw_cli.extract_agent_text(
        {
            "summary": summary,
            "payloads": [{"text": '{"status": "old"}'}, {"text": '{"status": "final"}'}],
        }
    )
    assert json.loads(out) == {"status": "final"}


def test_parse_prefers_embedded_json_over_yaml_prose():
    assert parse_agent_json_or_yaml('Result: here {"a": 1}') == {"a": 1}
    assert parse_agent_json_or_yaml('Result:\n  b: {"a": 1}\nfoo') != {}
    assert parse_agent_json_or_yaml('{"a": 1}') == {"a": 1}


def test_deep_researcher_non_frontmatter_garbage_raises():
    with pytest.raises(AgentOutputParseError):
        coerce_deep_researcher_state_machine("I could not find anything.", {"market_id": "m"})


def test_deep_researcher_error_and_unexpected_status():
    with pytest.raises(ValidationError, match="no data"):
        parse_deep_researcher_json({"status": "complete", "error": "no data"})
    with pytest.raises(ValidationError):
        parse_deep_researcher_json({"status": "weird"})
    ok = parse_deep_researcher_json(
        {
            "status": "complete", "market_id": "m", "estimated_p": 0.3, "p_low": 0.2,
            "p_high": 0.4, "confidence": "low", "resolution_checklist": [],
            "key_assumptions": [], "as_of": "2026-10-22", "markdown": "x", "error": None,
        }
    )
    assert ok.error is None


def test_directive_coercion():
    md = (
        "## Filter Weightings\n```yaml\nfilters:\n"
        "  gate_min_q: '0.07'\n  min_days_to_resolution: -5\n"
        "  research_max_q_gap: .inf\n  bogus: 1\n```\n"
    )
    from config.trading_constants import FILTERS

    out = extract_filter_directives(md)
    assert out["gate_min_q"] == 0.07
    assert out["min_days_to_resolution"] == FILTERS["min_days_to_resolution"]
    assert out["research_max_q_gap"] == FILTERS["research_max_q_gap"]


def test_select_q_order_and_range():
    assert select_q({"yes_price": 0.3, "midpoint": 0.4}) == 0.3
    assert select_q({"yes_price": 0.0, "last_trade_price": 1.0, "midpoint": 0.4}) == 0.4
    assert select_q({"yes_price": float("nan"), "midpoint": True}) is None
    assert select_q({}) is None
