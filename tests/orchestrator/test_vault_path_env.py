"""OPENCLAW_VAULT_PATH overrides the default Obsidian vault workspace root."""

from __future__ import annotations

from obsidian_utils import ObsidianManager
from config.vault import VAULT_PATH_ENV, resolve_vault_base


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
