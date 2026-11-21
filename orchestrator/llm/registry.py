"""Provider registry: a new provider = one LLM subclass + one entry here."""

from __future__ import annotations

from orchestrator.llm.anthropic_llm import AnthropicLLM
from orchestrator.llm.base import LLM
from orchestrator.llm.claude_code_llm import ClaudeCodeLLM
from orchestrator.llm.openrouter_llm import OpenRouterLLM

_CLASSES: list[type[LLM]] = [AnthropicLLM, ClaudeCodeLLM, OpenRouterLLM]
try:  # Gemini lands in its own module; skip it while absent.
    from orchestrator.llm.gemini_llm import GeminiLLM

    _CLASSES.append(GeminiLLM)
except ImportError:
    pass

PROVIDERS: dict[str, type[LLM]] = {cls.provider_name: cls for cls in _CLASSES}
