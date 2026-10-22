"""OpenClaw CLI adapter for local Gateway-backed agent runs."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from typing import Any

from orchestrator.config import (
    openclaw_agent_max_attempts,
    openclaw_agent_retry_backoff,
    openclaw_agent_timeout,
    openclaw_bin,
)
from orchestrator.parse import AgentOutputParseError, parse_agent_json_or_yaml

log = logging.getLogger(__name__)


class OpenClawCLIError(RuntimeError):
    """Raised when the OpenClaw CLI cannot run or returns unusable output."""


def _resolved_openclaw_bin() -> str:
    binary = openclaw_bin()
    resolved = shutil.which(binary)
    if resolved is None:
        raise OpenClawCLIError(
            f"OpenClaw binary {binary!r} not found on PATH. "
            "Install OpenClaw or set OPENCLAW_BIN."
        )
    return resolved


def gateway_reachable(timeout: int = 5) -> tuple[bool, str | None]:
    """Return whether the local OpenClaw Gateway responds to CLI status."""
    try:
        binary = _resolved_openclaw_bin()
    except OpenClawCLIError as exc:
        return False, str(exc)

    try:
        result = subprocess.run(
            [binary, "gateway", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"openclaw gateway status timed out after {timeout}s"
    except OSError as exc:
        return False, f"openclaw gateway status failed to launch: {exc}"
    if result.returncode == 0:
        return True, None
    detail = (result.stderr or result.stdout).strip()
    if not detail:
        detail = f"openclaw gateway status exited {result.returncode}"
    return False, detail


def require_gateway() -> None:
    """Raise a clear setup error if live mode cannot reach OpenClaw Gateway."""
    ok, detail = gateway_reachable()
    if ok:
        return
    message = (
        "OpenClaw Gateway is not reachable. Start it with `openclaw gateway` "
        "or OpenClaw Desktop. CMDOP agent is not required."
    )
    if detail:
        message = f"{message} Details: {detail}"
    raise OpenClawCLIError(message)


_SESSION_KEY_HELP_FLAG: bool | None = None


def _agent_cli_supports_session_key(binary: str) -> bool:
    """Detect whether this OpenClaw build exposes `openclaw agent --session-key`."""
    global _SESSION_KEY_HELP_FLAG
    if _SESSION_KEY_HELP_FLAG is not None:
        return _SESSION_KEY_HELP_FLAG

    try:
        result = subprocess.run(
            [binary, "agent", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OpenClawCLIError("openclaw agent --help probe timed out after 10s") from exc
    except OSError as exc:
        raise OpenClawCLIError(f"openclaw agent --help probe failed to launch: {exc}") from exc
    help_text = f"{result.stdout}\n{result.stderr}"
    supported = "--session-key" in help_text
    if result.returncode == 0:
        _SESSION_KEY_HELP_FLAG = supported
    return supported


def _scoped_session_suffix(session_key: str, agent_id: str) -> str:
    """Return the bare suffix used with `--agent` + `--session-key`."""
    prefix = f"agent:{agent_id}:"
    if session_key.startswith(prefix):
        return session_key[len(prefix) :]
    return session_key


def _agent_command(
    binary: str,
    agent_id: str,
    message: str,
    session_key: str,
    run_timeout: int,
) -> list[str]:
    """Build `openclaw agent` argv per https://docs.openclaw.ai/cli/agent."""
    base = [
        binary,
        "agent",
        "--message",
        message,
        "--json",
        "--timeout",
        str(run_timeout),
    ]
    if _agent_cli_supports_session_key(binary):
        # `--agent ops --session-key incident-42` -> `agent:ops:incident-42`
        return [
            *base[:2],
            "--agent",
            agent_id,
            "--session-key",
            _scoped_session_suffix(session_key, agent_id),
            *base[2:],
        ]

    # Older CLIs only accept `--session-id`. Omit `--agent` so the full key
    # selects the routed session instead of pinning to `agent:<id>:main`.
    return [
        *base[:2],
        "--session-id",
        session_key,
        *base[2:],
    ]


def _run_agent_once(
    binary: str,
    agent_id: str,
    message: str,
    session_key: str,
    run_timeout: int,
) -> dict[str, Any]:
    """Execute a single ``openclaw agent`` invocation."""
    # Built outside the run try-block so a help-probe timeout is not reported
    # as an agent timeout (the probe raises OpenClawCLIError itself).
    cmd = _agent_command(binary, agent_id, message, session_key, run_timeout)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=run_timeout + 30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OpenClawCLIError(
            f"openclaw agent timed out for {agent_id!r} after {run_timeout + 30}s"
        ) from exc
    except OSError as exc:
        raise OpenClawCLIError(
            f"openclaw agent failed to launch for {agent_id!r}: {exc}"
        ) from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise OpenClawCLIError(
            f"openclaw agent failed for {agent_id!r} "
            f"(exit {result.returncode}): {detail}"
        )

    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        parsed = None
        start = result.stdout.find("{")
        if start != -1:
            try:
                parsed, _ = json.JSONDecoder().raw_decode(result.stdout, start)
            except json.JSONDecodeError:
                parsed = None
        if parsed is None:
            raise OpenClawCLIError(
                f"openclaw agent returned non-JSON for {agent_id!r}: {exc}"
            ) from exc
    if not isinstance(parsed, dict):
        raise OpenClawCLIError(
            f"openclaw agent returned {type(parsed).__name__}, expected object"
        )
    return parsed


def run_agent(
    agent_id: str,
    message: str,
    *,
    session_key: str,
    timeout: int | None = None,
    retry: bool | None = None,
) -> dict[str, Any]:
    """Run an OpenClaw agent through the local Gateway and return CLI JSON.

    ``retry=None`` retries read-only roles but never the ``executioner`` (a
    retry could double-submit a trade); ``retry=False`` forces one attempt.
    """
    binary = _resolved_openclaw_bin()
    run_timeout = timeout or openclaw_agent_timeout()
    if retry is None:
        retry = agent_id != "executioner"
    max_attempts = openclaw_agent_max_attempts() if retry else 1
    backoff = openclaw_agent_retry_backoff()

    last_error: OpenClawCLIError | None = None
    for attempt in range(max_attempts):
        try:
            return _run_agent_once(
                binary, agent_id, message, session_key, run_timeout
            )
        except OpenClawCLIError as exc:
            last_error = exc
            if attempt >= max_attempts - 1:
                raise
            delay = backoff * (2**attempt)
            log.warning(
                "openclaw agent attempt %d/%d failed for %r: %s; retrying in %.1fs",
                attempt + 1,
                max_attempts,
                agent_id,
                exc,
                delay,
            )
            time.sleep(delay)

    assert last_error is not None
    raise last_error


def _parses_whole(candidate: str) -> bool | None:
    """True if ``candidate`` parses without the embedded-JSON fallback, False if only
    via the fallback, ``None`` if it does not parse at all."""
    try:
        parse_agent_json_or_yaml(candidate)
        # Re-check without the embedded fallback: a prose-wrapped object counts as partial.
        stripped = candidate.lstrip()
        if stripped.startswith(("{", "---", "```")):
            return True
        return False
    except AgentOutputParseError:
        return None


def extract_agent_text(cli_json: dict[str, Any]) -> str:
    """Normalize OpenClaw Gateway/embedded JSON responses into agent text.

    OpenClaw may return reasoning in ``summary`` and the final answer in
    ``payloads``. The LAST payload is the final answer and wins; the summary /
    text fields are only a fallback when no payload parses. Candidates that
    parse as a whole beat ones that only parse via embedded-JSON recovery.
    """
    payload_texts = _payload_text_list(cli_json)
    candidates = _collect_agent_text_candidates(cli_json)
    if not candidates:
        raise OpenClawCLIError("openclaw agent JSON did not contain text output")

    ordered: list[str] = []
    for text in [*reversed(payload_texts), *candidates]:
        if text not in ordered:
            ordered.append(text)

    partial: str | None = None
    for candidate in ordered:
        verdict = _parses_whole(candidate)
        if verdict is True:
            return candidate
        if verdict is False and partial is None:
            partial = candidate
    return partial if partial is not None else candidates[0]


def _collect_agent_text_candidates(cli_json: dict[str, Any]) -> list[str]:
    """Gather distinct non-empty text blobs from an OpenClaw CLI JSON object."""
    out: list[str] = []

    def add(text: str | None) -> None:
        if text and text.strip() and text.strip() not in out:
            out.append(text.strip())

    for key in ("text", "summary", "message", "output", "content"):
        add(_get_str(cli_json.get(key)))

    result = cli_json.get("result")
    if isinstance(result, dict):
        for key in ("text", "summary", "message", "output", "content"):
            add(_get_str(result.get(key)))
        add(_payloads_text(result.get("payloads")))

    add(_payloads_text(cli_json.get("payloads")))
    return out


def _payload_texts(payloads: Any) -> list[str]:
    if not isinstance(payloads, list):
        return []
    return [
        str(payload["text"]).strip()
        for payload in payloads
        if isinstance(payload, dict)
        and isinstance(payload.get("text"), str)
        and payload["text"].strip()
    ]


def _payload_text_list(cli_json: dict[str, Any]) -> list[str]:
    """Individual payload texts in order (result.payloads, then top-level payloads)."""
    out: list[str] = []
    result = cli_json.get("result")
    if isinstance(result, dict):
        out.extend(_payload_texts(result.get("payloads")))
    out.extend(_payload_texts(cli_json.get("payloads")))
    return out


def _payloads_text(payloads: Any) -> str | None:
    texts = _payload_texts(payloads)
    return "\n\n".join(texts) if texts else None


def _get_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def reset_agent_cli_capability_cache() -> None:
    """Clear cached `openclaw agent --help` detection (for tests)."""
    global _SESSION_KEY_HELP_FLAG
    _SESSION_KEY_HELP_FLAG = None


__all__ = [
    "OpenClawCLIError",
    "extract_agent_text",
    "gateway_reachable",
    "require_gateway",
    "reset_agent_cli_capability_cache",
    "run_agent",
]
