"""Parsing utilities for agent responses.

Agents may return raw JSON, raw YAML, or text that wraps either inside a
fenced code block (```json ... ``` / ```yaml ... ```). This module accepts
all of those shapes and raises :class:`AgentOutputParseError` on failure so
the orchestrator can route the market to the Dead Letter Queue.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import yaml

log = logging.getLogger(__name__)

_FENCE_RE = re.compile(
    r"^\s*```(?:json|yaml|yml)?\s*\n(?P<body>.*?)\s*```\s*$",
    re.DOTALL | re.IGNORECASE,
)
_FENCE_SEARCH_RE = re.compile(
    r"```(?:json|yaml|yml)?\s*\n(?P<body>.*?)\s*```",
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


def extract_fenced_block(text: str) -> str | None:
    """Return the body of the first embedded fenced block, if any."""
    match = _FENCE_SEARCH_RE.search(text.strip())
    return match.group("body").strip() if match else None


def _parse_body_candidates(raw: str) -> dict[str, Any] | None:
    """Try JSON/YAML on stripped text and embedded fenced blocks."""
    body = strip_code_fence(raw).strip()
    candidates: list[str] = []
    if body:
        candidates.append(body)
    fenced = extract_fenced_block(raw)
    if fenced and fenced not in candidates:
        candidates.insert(0, fenced)

    parsed: Any = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            try:
                parsed = yaml.safe_load(candidate)
            except yaml.YAMLError:
                parsed = None
        if isinstance(parsed, dict):
            return parsed
    return None


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

    if not raw.strip():
        raise AgentOutputParseError("agent returned empty output", raw=raw)

    parsed = _parse_body_candidates(raw)
    if parsed is not None:
        return parsed

    recovered = _find_embedded_json_object(raw)
    if recovered is not None:
        log.info("Recovered embedded JSON object from agent prose/fences")
        return recovered

    raise AgentOutputParseError("expected a mapping, got non-dict", raw=raw)


def _find_embedded_json_object(text: str) -> dict[str, Any] | None:
    """Return the first substring that decodes as a JSON object, else ``None``.

    Uses the stdlib scanner (no hand-rolled brace matching); ``raw_decode``
    cleanly ignores surrounding prose and trailing characters such as a
    closing code fence.
    """
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict):
            return obj
        start = text.find("{", start + 1)
    return None


def normalize_structured_output(
    role: str,
    payload: dict[str, Any],
    parsed: dict[str, Any],
    *,
    output_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Backfill known output fields from the orchestrator payload when missing."""
    schema = output_schema or {}
    if "market_id" not in schema:
        return parsed

    payload_mid = payload.get("market_id")
    if payload_mid and not parsed.get("market_id"):
        log.info(
            "[%s] backfilled market_id from orchestrator payload (agent omitted it)",
            role,
        )
        return {**parsed, "market_id": payload_mid}
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
    straight to ``flag_inactive``.
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
    "extract_fenced_block",
    "parse_agent_json_or_yaml",
    "normalize_structured_output",
    "coerce_deep_researcher_markdown",
    "agent_error_reason",
]