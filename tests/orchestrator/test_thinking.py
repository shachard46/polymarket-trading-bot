"""Per-call low thinking for quick mode + thinking_tokens accounting."""

from __future__ import annotations

import pytest

from orchestrator import direct_runner
from orchestrator.llm.base import LLMRequest, LLMResponse
from orchestrator.llm.gemini_llm import GeminiLLM
from orchestrator.llm.openrouter_llm import OpenRouterLLM
from orchestrator.stats.cost import cost_stats


def test_reasoning_for_payload():
    assert direct_runner._reasoning_for({"mode": "quick"}) is False
    assert direct_runner._reasoning_for({"mode": "full"}) is None
    assert direct_runner._reasoning_for({}) is None


def test_openrouter_override_wins_and_does_not_leak(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    llm = OpenRouterLLM("m")
    seen = []
    monkeypatch.setattr(llm, "_post", lambda url, h, body: seen.append(body) or {"choices": [{"message": {"content": "x"}}]})
    llm.complete("s", "u", reasoning=False)
    llm.complete("s", "u")
    assert seen[0]["reasoning"] == {"enabled": False} and "reasoning" not in seen[1]


def test_gemini_override_sets_low_thinking_in_sync_and_batch(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    llm = GeminiLLM("gemini-4-flash")
    sent = {}
    monkeypatch.setattr(llm, "_post", lambda url, h, body: sent.update(body=body) or {"name": "batches/1"})
    llm._batch_submit([LLMRequest("s", "a", reasoning=False), LLMRequest("s", "b")])
    cfgs = [r["request"]["generationConfig"] for r in sent["body"]["batch"]["input_config"]["requests"]["requests"]]
    assert cfgs[0]["thinkingConfig"] == {"thinkingLevel": "low"} and "thinkingConfig" not in cfgs[1]


def test_complete_many_fallback_passes_reasoning(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    llm = OpenRouterLLM("m")
    got = []
    monkeypatch.setattr(llm, "complete", lambda s, u, **kw: got.append(kw["reasoning"]) or LLMResponse("x", "p", "m"))
    llm.complete_many([LLMRequest("s", "u", reasoning=False)])
    assert got == [False]


def test_run_many_sets_reasoning_from_mode(monkeypatch):
    seen = []

    class Fake:
        def complete_many(self, reqs):
            seen.extend(r.reasoning for r in reqs)
            return [LLMResponse("{}", "p", "m") for _ in reqs]

    monkeypatch.setattr(direct_runner.routing, "llm_for", lambda role: Fake())
    monkeypatch.setattr(direct_runner, "_live_prompt", lambda *a: "p")
    direct_runner.run_many("deep_researcher", {}, [{"mode": "quick"}, {"mode": "full"}])
    assert seen == [False, None]


def test_thinking_tokens_parsed(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    r = OpenRouterLLM("m")._parse({"choices": [{"message": {"content": "x"}}],
                                   "usage": {"completion_tokens": 10, "completion_tokens_details": {"reasoning_tokens": 7}}})
    assert r.output_tokens == 10 and r.thinking_tokens == 7
    assert OpenRouterLLM("m")._parse({"choices": [{"message": {"content": "x"}}]}).thinking_tokens is None
    g = GeminiLLM("gemini-3")._parse({"candidates": [{"content": {"parts": [{"text": "x"}]}}],
                                      "usageMetadata": {"candidatesTokenCount": 3, "thoughtsTokenCount": 5}})
    assert g.output_tokens == 8 and g.thinking_tokens == 5


def test_cost_stats_thinking_by_role():
    c = cost_stats([], [{"role": "a", "thinking_tokens": 5}, {"role": "a", "thinking_tokens": None}, {"role": "b"}])
    assert c["thinking_by_role"] == {"a": 5, "b": 0}
