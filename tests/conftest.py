"""Repo-wide test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _tmp_vault_env(monkeypatch, tmp_path_factory):
    """Default vault base is a tmp dir, so code that resolves it (e.g. LLM usage rows) never writes into the repo."""
    monkeypatch.setenv("PMBOT_VAULT_PATH", str(tmp_path_factory.mktemp("vault_env")))


@pytest.fixture(autouse=True)
def _default_two_judges(monkeypatch):
    """Tests assume the two-judge arm unless they pick another (the shipped default is the per-market A/B)."""
    monkeypatch.setenv("JUDGE_ARM_MODE", "two")


@pytest.fixture(autouse=True)
def _no_source_cache():
    """The classifier cache is bound per pipeline run; never let one test's file leak into the next."""
    from orchestrator import source_quality

    source_quality.bind_cache(None)
    yield
    source_quality.bind_cache(None)
