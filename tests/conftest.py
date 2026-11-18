"""Repo-wide test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _tmp_vault_env(monkeypatch, tmp_path_factory):
    """Default vault base is a tmp dir, so code that resolves it (e.g. LLM usage rows) never writes into the repo."""
    monkeypatch.setenv("PMBOT_VAULT_PATH", str(tmp_path_factory.mktemp("vault_env")))
