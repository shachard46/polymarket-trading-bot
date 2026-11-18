"""Shared orchestrator test fixtures."""

from __future__ import annotations

from typing import Any

import pytest

from obsidian_utils import ObsidianManager
from orchestrator import polymarket_api, scraper

# Per-test hints for the default fake CLOB quotes: market_id -> market_data (see _fake_quote).
QUOTE_HINTS: dict[str, dict[str, Any]] = {}


def _fake_quote(market_id: str, side: str) -> dict[str, Any] | None:
    """Default executable quote for phase tests: ask = the row's yes/no ask (else price), deep book."""
    md = QUOTE_HINTS.get(market_id)
    if md is None:
        row = scraper.fetch_market_row(market_id)
        md = row.market_data if row is not None else None
    if not md:
        return None
    yes = md.get("yes_ask", md.get("yes_price"))
    if side == "YES":
        ask = yes
    else:
        ask = md.get("no_ask", md.get("no_price", None if yes is None else 1.0 - yes))
    if ask is None:
        return None
    return {"ask": ask, "depth_usd_within_2c": 1e9, "spread": 0.01, "token_id": f"tok-{side}"}


@pytest.fixture(autouse=True)
def _stub_clob_quotes(monkeypatch):
    """Phase tests never touch the network: executable quotes come from the row under test."""
    QUOTE_HINTS.clear()
    monkeypatch.setattr(polymarket_api, "executable_quote", _fake_quote)
    yield
    QUOTE_HINTS.clear()


@pytest.fixture(autouse=True)
def _no_holder_network(monkeypatch):
    """No test hits the network for holder concentration; tests that need it override."""
    monkeypatch.setattr(polymarket_api, "fetch_holder_concentration", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _no_integrity_network(monkeypatch):
    """No test hits the network for price history / trades / book; tests that need them override."""
    monkeypatch.setattr(polymarket_api, "fetch_price_history", lambda *a, **k: None)
    monkeypatch.setattr(polymarket_api, "fetch_trades", lambda *a, **k: None)
    monkeypatch.setattr(polymarket_api, "fetch_book", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _aiq_evidence_by_default(monkeypatch):
    """Existing phase tests exercise the A-IQ path; evidence tests override the env."""
    monkeypatch.setenv("PMBOT_EVIDENCE_SOURCE", "aiq")


@pytest.fixture()
def vault(tmp_path):
    """Cold-started ObsidianManager rooted at tmp_path (override locally for variants)."""
    v = ObsidianManager(vault_base=tmp_path)
    v.cold_start_protocol()
    return v


# Fixed provider/model config for tests: the shipped config/models.yaml is user-edited
# (providers and models change), so tests never read it unless they opt in explicitly.
TEST_MODELS_YAML = """\
max_tokens: 16000
web_max_uses: 8
default: {provider: anthropic, model: claude-sonnet-5-5}
roles:
  triage:          {provider: anthropic, model: claude-haiku-5-5}
  briefer:         {provider: anthropic, model: claude-sonnet-5-5}
  web_searcher:    {provider: anthropic, model: claude-sonnet-5-5, web_max_uses: 5}
  deep_researcher: {provider: anthropic, model: claude-sonnet-5-5}
  post_mortem_analyst: {provider: anthropic, model: claude-sonnet-5-5}
  overseer:        {provider: anthropic, model: claude-opus-5-5}
  concierge:       {provider: anthropic, model: claude-haiku-5-5}
"""


@pytest.fixture(autouse=True)
def _fixed_models_config(monkeypatch, tmp_path_factory):
    p = tmp_path_factory.getbasetemp() / "test_models.yaml"
    if not p.exists():
        p.write_text(TEST_MODELS_YAML)
    monkeypatch.setenv("LLM_MODELS_CONFIG", str(p))
