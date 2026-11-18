"""Regression tests for the vault / parsing / CLI hardening pass."""

from __future__ import annotations

import os
import subprocess

import pytest
from pydantic import ValidationError

from config.vault import VAULT_PATH_ENV, resolve_vault_base
from obsidian_utils import ObsidianManager, VaultWriteError, _atomic_write_text
from orchestrator.agent_outputs import parse_deep_researcher_json
from orchestrator.parse import (
    AgentOutputParseError,
    coerce_deep_researcher_state_machine,
    parse_agent_json_or_yaml,
)
from orchestrator.research import split_yaml_frontmatter_markdown
from orchestrator.schema_validation import (
    AgentSchemaError,
    build_model,
    validate_payload,
)


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


def test_resolve_vault_base_uses_env(monkeypatch, tmp_path):
    custom = tmp_path / "vault_workspace"
    custom.mkdir()
    monkeypatch.setenv(VAULT_PATH_ENV, str(custom))

    assert resolve_vault_base() == custom.resolve()


def test_explicit_vault_base_overrides_env(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    monkeypatch.setenv(VAULT_PATH_ENV, str(tmp_path / "ignored"))

    assert resolve_vault_base(explicit) == explicit.resolve()


def test_obsidian_manager_uses_env_when_vault_base_omitted(monkeypatch, tmp_path):
    custom = tmp_path / "vault_workspace"
    custom.mkdir()
    monkeypatch.setenv(VAULT_PATH_ENV, str(custom))

    vault = ObsidianManager()
    assert vault._base == custom.resolve()
    assert vault._dirs["system"] == (custom / "Vault" / "00_System").resolve()
