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
title: Active Directives
---

# Active Directives

## Research Protocol

Body.

## Filter Weightings

```yaml
filters:
  gate_min_q: 0.05
```

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


def test_phase6_quarantines_malformed_overseer_output(tmp_path, monkeypatch):
    monkeypatch.setenv("OVERSEER_MIN_NEW_RESOLVED", "0")
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

    # No separate error vault — prior directives are retained in place.
    assert (tmp_path / "Vault" / "05_Errors").exists() is False


def test_phase6_writes_directives_when_valid(tmp_path, monkeypatch):
    monkeypatch.setenv("OVERSEER_MIN_NEW_RESOLVED", "0")
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
    # the Hub stamps the next version; the Overseer's body is kept verbatim
    assert "version: 2" in vault.read_directives()
    assert "# Active Directives\n\n## Research Protocol\n\nBody." in vault.read_directives()
