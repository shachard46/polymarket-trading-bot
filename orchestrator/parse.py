"""Parsing utilities for agent responses.

Agents may return raw JSON, raw YAML, or text that wraps either inside a
fenced code block (```json ... ``` / ```yaml ... ```). This module accepts
all of those shapes and raises :class:`AgentOutputParseError` on failure so
the orchestrator can route the market to the Dead Letter Queue.
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

_FENCE_RE = re.compile(
    r"^\s*```(?:json|yaml|yml)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL | re.IGNORECASE,
)


class AgentOutputParseError(ValueError):
    """Raised when an agent response cannot be parsed into a mapping."""

    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


def strip_code_fence(text: str) -> str:
    """Return ``text`` with a single surrounding fenced code block stripped.

    No-op when the text is not wrapped in a fence.
    """
    match = _FENCE_RE.match(text.strip())
    return match.group("body") if match else text


def parse_agent_json_or_yaml(raw: str | dict[str, Any] | None) -> dict[str, Any]:
    """Parse an agent response into a dict.

    Accepts an already-parsed mapping (passthrough), a JSON string, a YAML
    string, or either wrapped in a fenced code block. Raises
    :class:`AgentOutputParseError` if the result is not a mapping.
    """
    if raw is None:
        raise AgentOutputParseError("agent returned no output", raw="")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise AgentOutputParseError(
            f"unsupported response type: {type(raw).__name__}", raw=repr(raw)
        )

    body = strip_code_fence(raw).strip()
    if not body:
        raise AgentOutputParseError("agent returned empty output", raw=raw)

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        try:
            parsed = yaml.safe_load(body)
        except yaml.YAMLError as exc:
            raise AgentOutputParseError(
                f"response is neither valid JSON nor YAML: {exc}", raw=raw
            ) from exc

    if not isinstance(parsed, dict):
        raise AgentOutputParseError(
            f"expected a mapping, got {type(parsed).__name__}", raw=raw
        )
    return parsed


def coerce_deep_researcher_markdown(raw: Any) -> str:
    """Normalize a Deep Researcher response into a markdown string.

    Live OpenClaw responses for ``deep_researcher`` should be a raw markdown
    string (frontmatter + body). Some transports wrap the payload as
    ``{"markdown": "..."}`` or ``{"content": "..."}``; both are unwrapped
    here. Anything else raises :class:`AgentOutputParseError`.
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise AgentOutputParseError("deep researcher returned empty markdown", raw=raw)
        return text
    if isinstance(raw, dict):
        for key in ("markdown", "content", "text", "output"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        raise AgentOutputParseError(
            "deep researcher dict missing markdown/content field",
            raw=json.dumps(raw, default=str),
        )
    raise AgentOutputParseError(
        f"deep researcher returned unsupported type: {type(raw).__name__}",
        raw=repr(raw),
    )


def agent_error_reason(payload: dict[str, Any] | None) -> str | None:
    """Return the error string from an agent payload, if any.

    Treats missing key, ``None``, and empty string as "no error". Any other
    value is coerced to ``str`` and returned so the caller can pass it
    straight to the DLQ.
    """
    if not payload:
        return None
    err = payload.get("error")
    if err is None:
        return None
    text = str(err).strip()
    return text or None


__all__ = [
    "AgentOutputParseError",
    "strip_code_fence",
    "parse_agent_json_or_yaml",
    "coerce_deep_researcher_markdown",
    "agent_error_reason",
]
