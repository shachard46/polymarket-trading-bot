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
from typing import Any, Callable, Iterable, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from orchestrator.config import MAX_FORMAT_RETRIES
from orchestrator.schema_validation import AgentSchemaError

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

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
        if isinstance(parsed, str):
            try:
                nested = json.loads(parsed)
            except json.JSONDecodeError:
                nested = None
            if isinstance(nested, dict):
                return nested
    return None


def _garbage_keys(obj: dict[str, Any]) -> bool:
    """True when a key looks like leaked wrapper/marker text (a fence, brace or angle bracket) rather than a field name."""
    return any(isinstance(k, str) and re.search(r"[`{}<>]", k) for k in obj)


def _embedded_json_objects(text: str) -> list[dict[str, Any]]:
    """Every balanced JSON object in ``text`` (outermost first; objects nested in a decoded one are skipped)."""
    decoder = json.JSONDecoder()
    out: list[dict[str, Any]] = []
    pos = text.find("{")
    while pos != -1:
        try:
            obj, end = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            pos = text.find("{", pos + 1)
            continue
        if isinstance(obj, dict):
            out.append(obj)
            pos = text.find("{", end)
        else:
            pos = text.find("{", pos + 1)
    return out


def _prefer_expected(
    raw: str, parsed: dict[str, Any] | None, expected_keys: Iterable[str]
) -> dict[str, Any] | None:
    """Pick, among the plain parse and every embedded JSON object, the one overlapping most with ``expected_keys``.

    Returns ``None`` when no candidate shares a key with ``expected_keys``. A single-key wrapper whose value is a dict
    also offers that inner dict (one level).

    Candidates with garbage keys (marker text parsed as a YAML key) score below clean ones; ties keep the earlier one.
    """
    wanted = set(expected_keys)
    cands = []
    for c in ([parsed] if parsed is not None else []) + _embedded_json_objects(raw):
        cands.append(c)
        if len(c) == 1 and isinstance(next(iter(c.values())), dict):  # single-key wrapper: the inner object too
            cands.append(next(iter(c.values())))
    if not cands:
        return None

    def score(c: dict[str, Any]) -> tuple[int, int]:
        return (len(wanted & set(map(str, c))), 0 if _garbage_keys(c) else 1)

    best = max(cands, key=score)  # max keeps the first of equals
    return best if score(best)[0] >= 1 else None  # no overlap: caller keeps the old logic


def parse_agent_json_or_yaml(
    raw: str | dict[str, Any] | None, expected_keys: Iterable[str] | None = None
) -> dict[str, Any]:
    """Parse an agent response into a dict.

    ``expected_keys`` (the role's output field names) makes the parse schema-aware: leading marker text and code fences
    are ignored and, of all JSON objects found, the one matching the schema best wins.

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
    if parsed is not None and _garbage_keys(parsed):
        parsed = None  # marker text such as '>>x}```json' parsed as a YAML key: never a real payload
    if expected_keys:
        picked = _prefer_expected(raw, parsed, expected_keys)
        if picked is not None:
            return picked
    if parsed is not None:
        structured_start = raw.lstrip().startswith(("{", "---", "```"))
        single_key_wrapper = len(parsed) == 1 and isinstance(
            next(iter(parsed.values())), dict
        )
        if not structured_start or single_key_wrapper:
            # YAML happily parses prose ("Result: {...}"); prefer a real embedded object.
            embedded = _find_embedded_json_object(raw)
            if embedded is not None:
                log.info("Preferred embedded JSON object over YAML prose parse")
                return embedded
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
    if role == "briefer" and "research_queries" not in parsed and parsed.get("summary"):
        raise ValueError(
            "legacy field 'summary' removed; emit research_queries (4 fixed-slot strings). "
            "Hub runs A-IQ from research_queries — you do not call tools."
        )

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


def coerce_deep_researcher_state_machine(
    raw: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Validate that a Deep Researcher response is a JSON state-machine object.

    Requires ``status`` of ``needs_more_data`` or ``complete``; anything else raises
    :class:`AgentOutputParseError` (which drives the format retry).
    """
    if isinstance(raw, dict):
        status = raw.get("status")
        if status in ("needs_more_data", "complete"):
            return raw
        raise AgentOutputParseError(
            f"deep researcher unexpected status: {status!r} (expected a JSON object with "
            "status needs_more_data or complete)",
            raw=json.dumps(raw, default=str),
        )
    raise AgentOutputParseError(
        "deep researcher output is not a JSON state-machine object "
        f"(got {type(raw).__name__})",
        raw=raw if isinstance(raw, str) else repr(raw),
    )


def coerce_deep_researcher_markdown(raw: Any) -> str:
    """Normalize a Deep Researcher response into a markdown string.

    Live ``deep_researcher`` responses should be a raw markdown
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


def preprocess_agent_text(raw: str) -> str:
    """Canonical pre-parser: strip fences and prefer embedded fenced JSON bodies."""
    text = strip_code_fence(raw).strip()
    fenced = extract_fenced_block(raw)
    if fenced:
        return fenced
    return text


def _role_keys(role: str) -> list[str]:
    """Output field names declared by the role's ``output_schema`` (empty when unknown)."""
    try:
        import agents_blueprint  # noqa: PLC0415 - lazy: avoids an import cycle

        schema = agents_blueprint.AGENTS[role].get("output_schema")
    except Exception:  # noqa: BLE001
        return []
    return list(schema) if isinstance(schema, dict) else []


def junk_payload_reason(parsed: Any) -> str | None:
    """Why a parsed agent mapping is a degenerate model output (junk keys / empty), else ``None``.

    Some models emit valid JSON with punctuation-only keys (e.g. ``{": ": null, ...}``) that the schema may
    still tolerate (extra keys are allowed); such output is a format failure, not an answer.
    """
    if not isinstance(parsed, dict):
        return None
    if not parsed:
        return "empty JSON object"
    junk = [k for k in parsed if not isinstance(k, str) or not any(ch.isalnum() for ch in k)]
    if junk:
        return f"junk keys {junk!r} (keys must be the schema field names)"
    return None


def run_agent_with_format_retries(
    runner: Callable[[str, dict[str, Any]], Any],
    role: str,
    payload: dict[str, Any],
    *,
    validate_fn: Callable[[dict[str, Any]], T],
    max_retries: int = MAX_FORMAT_RETRIES,
    parse_response: Callable[[Any], dict[str, Any]] | None = None,
) -> T:
    """Invoke ``runner`` until ``validate_fn`` accepts the parsed JSON mapping.

    On :class:`ValidationError`, append ``format_validation_error`` to the payload
    and retry up to ``max_retries`` times after the first failure.

    Optional ``parse_response`` maps the raw agent return value to a dict before
    ``validate_fn`` (e.g. legacy markdown coercion for Deep Researcher).
    """
    attempt_payload = dict(payload)
    last_error: ValidationError | None = None
    max_attempts = max_retries + 1

    for attempt in range(max_attempts):
        try:
            raw = runner(role, attempt_payload)
        except AgentSchemaError as exc:
            # Output-schema mismatch is a format error; an input mismatch is a Hub bug.
            if exc.direction != "output" or attempt >= max_retries:
                raise
            attempt_payload = {**payload, "format_validation_error": str(exc)}
            continue
        try:
            if parse_response is not None:
                parsed = parse_response(raw)
            elif isinstance(raw, dict):
                parsed = raw
            else:
                text = raw if isinstance(raw, str) else str(raw)
                parsed = parse_agent_json_or_yaml(preprocess_agent_text(text), expected_keys=_role_keys(role))
            return validate_fn(parsed)
        except AgentOutputParseError as exc:
            if attempt >= max_retries:
                raise
            attempt_payload = {
                **payload,
                "format_validation_error": str(exc),
            }
        except ValidationError as exc:
            last_error = exc
            if attempt >= max_retries:
                break
            attempt_payload = {
                **payload,
                "format_validation_error": str(exc),
            }

    assert last_error is not None
    raise last_error


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
    "preprocess_agent_text",
    "parse_agent_json_or_yaml",
    "normalize_structured_output",
    "coerce_deep_researcher_markdown",
    "coerce_deep_researcher_state_machine",
    "run_agent_with_format_retries",
    "junk_payload_reason",
    "agent_error_reason",
]