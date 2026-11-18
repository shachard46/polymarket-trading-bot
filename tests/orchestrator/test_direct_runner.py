"""Live-mode seam: runner routing, run_direct/complete over routing, ledger + stats wiring."""

from __future__ import annotations

from typing import Any

import pytest

import agents_blueprint
from orchestrator import config, direct_runner, runner
from orchestrator.llm import routing
from orchestrator.llm.base import LLM, LLMError, LLMResponse


class _Rec(LLM):
    provider_name, api_key_env = "rec", "REC_KEY"
    calls: list[tuple] = []

    def _send(self, system, user, json_mode, web):
        _Rec.calls.append((self.model, system, user, json_mode, web))
        return LLMResponse("out", "rec", self.model, 1, 2)

    def _is_retryable(self, exc):
        return False


@pytest.fixture(autouse=True)
def _fake_sdk(monkeypatch):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "anthropic", types.ModuleType("anthropic"))


@pytest.fixture()
def rec(monkeypatch):
    from orchestrator.llm import registry

    monkeypatch.setitem(registry.PROVIDERS, "rec", _Rec)
    monkeypatch.setenv("LLM_DEFAULT", "rec:dflt")
    for role in agents_blueprint.AGENTS:
        monkeypatch.delenv(f"LLM_ROLE_{role.upper()}", raising=False)
    monkeypatch.setenv("LLM_MODELS_CONFIG", str(_empty_cfg(monkeypatch)))
    _Rec.calls = []
    return _Rec


def _empty_cfg(monkeypatch):
    import tempfile
    from pathlib import Path

    p = Path(tempfile.mkdtemp()) / "m.yaml"
    p.write_text("default: {provider: anthropic, model: x}\n")
    return p


def _payload(role="triage"):
    fields = agents_blueprint.AGENTS[role]["input_model"].model_fields
    return {k: "x" for k in fields}


def test_run_direct_json_and_web_flags(rec):
    out = direct_runner.run_direct("triage", agents_blueprint.AGENTS["triage"], _payload())
    assert out == "out"
    model, system, user, json_mode, web = rec.calls[0]
    assert model == "dflt" and json_mode is True and web is False
    assert system == agents_blueprint.AGENTS["triage"]["system_prompt"] and "Input JSON" in user
    direct_runner.run_direct(
        "web_searcher", agents_blueprint.AGENTS["web_searcher"],
        {"market_id": "0x1", "queries": ["q"], "today": "2026-01-01"},
    )
    assert rec.calls[1][4] is True


def test_only_web_searcher_gets_web():
    for role, spec in agents_blueprint.AGENTS.items():
        assert direct_runner._web_for(spec) is (role == "web_searcher"), role


def test_complete_no_tools_no_json(rec):
    assert direct_runner.complete("concierge", "SYS", "USER") == "out"
    assert rec.calls == [("dflt", "SYS", "USER", False, False)]


def test_require_api_key_delegates_to_routing(monkeypatch):
    from orchestrator.llm.registry import PROVIDERS

    monkeypatch.setenv("LLM_ROLE_TRIAGE", "anthropic:claude-haiku-5-5")  # pin one anthropic role
    for env in {cls.api_key_env for cls in PROVIDERS.values()}:
        monkeypatch.setenv(env, "k")
    direct_runner.require_api_key()
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(direct_runner.DirectRunnerError, match="ANTHROPIC_API_KEY"):
        direct_runner.require_api_key()
    assert direct_runner.DirectRunnerError is LLMError


def test_spawn_agent_live_routes_to_run_direct(monkeypatch):
    monkeypatch.setenv(config.RUNNER_MODE_ENV, "live")
    seen = {}

    def fake_run_direct(role, spec, payload):
        seen["role"] = role
        return ""

    monkeypatch.setattr(direct_runner, "run_direct", fake_run_direct)
    runner.spawn_agent("triage", _payload())
    assert seen["role"] == "triage"


def test_direct_is_no_longer_a_mode(monkeypatch):
    monkeypatch.setenv(config.RUNNER_MODE_ENV, "direct")
    with pytest.raises(ValueError):
        config.runner_mode()
    monkeypatch.setenv(config.RUNNER_MODE_ENV, "live")
    assert config.runner_mode() == "live"


def test_ledger_fields_provider_and_model(monkeypatch):
    from orchestrator.phases.common import _ledger_market_fields

    monkeypatch.setenv(config.RUNNER_MODE_ENV, "stub")
    f = _ledger_market_fields({}, role="triage")
    assert f["runner_backend"] == "stub" and "model" not in f and "provider" not in f
    monkeypatch.setenv(config.RUNNER_MODE_ENV, "live")
    assert "model" not in _ledger_market_fields({})
    f = _ledger_market_fields({}, role="triage")
    assert f["runner_backend"] == "live" and f["provider"] == "anthropic" and f["model"] == "claude-haiku-5-5"
    monkeypatch.setenv("LLM_ROLE_DEEP_RESEARCHER", "openrouter:vendor/m")
    f = _ledger_market_fields({}, role="deep_researcher")
    assert f["provider"] == "openrouter" and f["model"] == "vendor/m"


def test_stats_brier_by_model_and_provider():
    from orchestrator import ledger
    from orchestrator.stats import compute_stats, render_stats

    rows = [
        {"market_id": "a", "stage": "traded", "p": 0.8, "q": 0.5, "model": "m1", "provider": "anthropic", "ts": "2026-01-01T00:00:00Z"},
        {"market_id": "b", "stage": "traded", "p": 0.2, "q": 0.5, "model": "m2", "provider": "openrouter", "ts": "2026-01-01T00:00:00Z"},
        {"market_id": "a", "stage": ledger.RESOLUTION_STAGE, "outcome_yes": 1, "ts": "2026-01-02T00:00:00Z"},
        {"market_id": "b", "stage": ledger.RESOLUTION_STAGE, "outcome_yes": 1, "ts": "2026-01-02T00:00:00Z"},
    ]
    s = compute_stats(rows)
    assert s["brier_by_model"]["m1"][1] == pytest.approx(0.04)
    assert s["brier_by_provider"]["openrouter"][1] == pytest.approx(0.64)
    out = render_stats(s)
    assert "### Brier by model" in out and "### Brier by provider" in out and "openrouter" in out
