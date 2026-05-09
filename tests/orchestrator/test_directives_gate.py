"""F3 regression: Overseer output is structurally validated before overwriting directives."""

from __future__ import annotations

from typing import Any

import pytest

from obsidian_utils import (
    DirectivesPayload,
    ObsidianManager,
    VaultWriteError,
)
from orchestrator import phases


_VALID_DIRECTIVES = """\
---
version: "1.0"
overseer_updated: true
---

# Active Directives

## Research Protocol

Body.

## Filter Weightings

Body.

## Risk Constraints

Body.

## Output Requirements

Body.
"""


def test_seed_directives_pass_structural_validation(tmp_path):
    vault = ObsidianManager(vault_base=tmp_path)
    assert vault.cold_start_protocol() is True
    seed = vault.read_directives()
    DirectivesPayload(new_directives_markdown=seed, rationale="seed")


def test_directives_payload_rejects_missing_frontmatter():
    with pytest.raises(Exception):
        DirectivesPayload(
            new_directives_markdown="## Research Protocol\n\nbody",
            rationale="x",
        )


def test_directives_payload_rejects_missing_required_header():
    bad = _VALID_DIRECTIVES.replace("## Risk Constraints", "## Bogus Header")
    with pytest.raises(Exception):
        DirectivesPayload(new_directives_markdown=bad, rationale="x")


def test_directives_payload_accepts_valid_doc():
    DirectivesPayload(new_directives_markdown=_VALID_DIRECTIVES, rationale="ok")


def test_phase6_quarantines_malformed_overseer_output(tmp_path):
    vault = ObsidianManager(vault_base=tmp_path)
    vault.cold_start_protocol()
    seed_before = vault.read_directives()

    def bad_overseer(role: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "new_directives_markdown": "totally free-form garbage with no frontmatter",
            "rationale": "I felt creative",
            "error": None,
        }

    phases.phase6_macro_learning_loop(vault, runner=bad_overseer)

    # Directives must NOT have been overwritten.
    assert vault.read_directives() == seed_before

    # And the rejected payload must be archived in the DLQ.
    err_files = list((tmp_path / "Vault" / "05_Errors").glob("__overseer__*.json"))
    assert err_files, "expected an overseer DLQ artifact"


def test_phase6_writes_directives_when_valid(tmp_path):
    vault = ObsidianManager(vault_base=tmp_path)
    vault.cold_start_protocol()

    def good_overseer(role: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "new_directives_markdown": _VALID_DIRECTIVES,
            "rationale": "calibrated",
            "error": None,
        }

    phases.phase6_macro_learning_loop(vault, runner=good_overseer)
    assert "## Research Protocol" in vault.read_directives()
    assert vault.read_directives() == _VALID_DIRECTIVES
