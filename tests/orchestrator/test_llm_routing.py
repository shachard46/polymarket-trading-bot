"""models.yaml loading, env overrides and startup validation."""

from __future__ import annotations

import pytest

import agents_blueprint
from orchestrator.llm import routing
from orchestrator.llm.anthropic_llm import AnthropicLLM
from orchestrator.llm.base import LLMError
from orchestrator.llm.openrouter_llm import OpenRouterLLM
from orchestrator.llm.registry import PROVIDERS


@pytest.fixture(autouse=True)
def _fake_sdk(monkeypatch):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "anthropic", types.ModuleType("anthropic"))
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", types.ModuleType("claude_agent_sdk"))


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_DEFAULT", raising=False)
    for role in agents_blueprint.AGENTS:
        monkeypatch.delenv(f"LLM_ROLE_{role.upper()}", raising=False)
    for k in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.setenv(k, "k")


def _cfg(monkeypatch, tmp_path, text):
    p = tmp_path / "models.yaml"
    p.write_text(text)
    monkeypatch.setenv("LLM_MODELS_CONFIG", str(p))


def test_shipped_config_is_valid(monkeypatch):
    """The shipped models.yaml parses and every role resolves to a registered provider.

    Checks structure only, so editing a role's provider/model in the file never breaks it.
    """
    monkeypatch.delenv("LLM_MODELS_CONFIG")  # opt in to the real shipped file
    assert PROVIDERS["anthropic"] is AnthropicLLM
    roles = ("triage", "briefer", "web_searcher", "deep_researcher",
             "post_mortem_analyst", "overseer", "concierge")
    for role in roles:
        spec = routing.spec_for(role)
        assert spec.provider in PROVIDERS and spec.model
        assert spec.max_tokens > 0
        monkeypatch.setenv(PROVIDERS[spec.provider].api_key_env, "k")
    routing.validate()


def test_llm_for_builds_and_caches(monkeypatch):
    a = routing.llm_for("triage")
    assert isinstance(a, AnthropicLLM) and a.model == "claude-haiku-5-5"
    assert routing.llm_for("triage") is a
    monkeypatch.setenv("LLM_ROLE_TRIAGE", "openrouter:vendor/x:free")
    b = routing.llm_for("triage")
    assert isinstance(b, OpenRouterLLM) and b.model == "vendor/x:free"


def test_yaml_override_and_default(monkeypatch, tmp_path):
    _cfg(monkeypatch, tmp_path, """
max_tokens: 100
default: {provider: openrouter, model: vendor/d}
roles:
  triage: {provider: anthropic, model: t, max_tokens: 5}
""")
    assert routing.spec_for("triage").max_tokens == 5
    s = routing.spec_for("overseer")
    assert (s.provider, s.model, s.max_tokens) == ("openrouter", "vendor/d", 100)
    monkeypatch.setenv("LLM_DEFAULT", "anthropic:dd")
    assert routing.spec_for("overseer").model == "dd"
    assert routing.spec_for("triage").model == "t"  # own entry beats LLM_DEFAULT
    monkeypatch.setenv("LLM_ROLE_TRIAGE", "openrouter:r")
    assert routing.spec_for("triage").model == "r"  # role env beats yaml


@pytest.mark.parametrize("text,match", [
    ("default: {provider: nope, model: m}\n", "unknown provider"),
    ("default: {provider: anthropic, model: m}\nroles:\n  ghost: {provider: anthropic, model: m}\n", "unknown role"),
    ("default: {provider: anthropic}\n", "invalid models config"),
    ("default: {provider: anthropic, model: m}\nextra: 1\n", "invalid models config"),
])
def test_bad_config_raises(monkeypatch, tmp_path, text, match):
    _cfg(monkeypatch, tmp_path, text)
    with pytest.raises(LLMError, match=match):
        routing.spec_for("triage")


def test_missing_file_and_bad_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODELS_CONFIG", str(tmp_path / "none.yaml"))
    with pytest.raises(LLMError, match="cannot read"):
        routing.spec_for("triage")
    monkeypatch.delenv("LLM_MODELS_CONFIG")
    monkeypatch.setenv("LLM_ROLE_TRIAGE", "nomodel")
    with pytest.raises(LLMError, match="provider:model"):
        routing.spec_for("triage")
    monkeypatch.setenv("LLM_ROLE_TRIAGE", "bogus:m")
    with pytest.raises(LLMError, match="unknown provider"):
        routing.spec_for("triage")


def test_unknown_role_raises(monkeypatch):
    with pytest.raises(LLMError, match="unknown role"):
        routing.spec_for("nobody")
    monkeypatch.setenv("LLM_ROLE_NOBODY", "anthropic:m")
    with pytest.raises(LLMError, match="LLM_ROLE_NOBODY"):
        routing.validate()


def test_validate_missing_key_names_provider_and_roles(monkeypatch):
    monkeypatch.setenv("LLM_ROLE_TRIAGE", "openrouter:m")
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(LLMError) as ei:
        routing.validate()
    msg = str(ei.value)
    assert "OPENROUTER_API_KEY" in msg and "triage" in msg


def test_validate_skips_unused_provider_and_concierge(monkeypatch):
    monkeypatch.setenv("LLM_ROLE_CONCIERGE", "openrouter:m")  # not hub-spawned
    monkeypatch.delenv("OPENROUTER_API_KEY")
    routing.validate()
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        routing.validate()
