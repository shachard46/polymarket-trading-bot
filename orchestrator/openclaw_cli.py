"""OpenClaw CLI adapter for local Gateway-backed agent runs."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Any

from orchestrator.config import openclaw_agent_timeout, openclaw_bin

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

    result = subprocess.run(
        [binary, "gateway", "status", "--json"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
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


def run_agent(
    agent_id: str,
    message: str,
    *,
    session_key: str,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Run an OpenClaw agent through the local Gateway and return CLI JSON."""
    binary = _resolved_openclaw_bin()
    run_timeout = timeout or openclaw_agent_timeout()
    result = subprocess.run(
        [
            binary,
            "agent",
            "--agent",
            agent_id,
            "--session-key",
            session_key,
            "--message",
            message,
            "--json",
            "--timeout",
            str(run_timeout),
        ],
        capture_output=True,
        text=True,
        timeout=run_timeout + 30,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise OpenClawCLIError(
            f"openclaw agent failed for {agent_id!r} "
            f"(exit {result.returncode}): {detail}"
        )

    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenClawCLIError(
            f"openclaw agent returned non-JSON for {agent_id!r}: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise OpenClawCLIError(
            f"openclaw agent returned {type(parsed).__name__}, expected object"
        )
    return parsed


def extract_agent_text(cli_json: dict[str, Any]) -> str:
    """Normalize OpenClaw Gateway/embedded JSON responses into agent text."""
    direct = _first_text(
        cli_json,
        ("text", "summary", "message", "output", "content"),
    )
    if direct:
        return direct

    result = cli_json.get("result")
    if isinstance(result, dict):
        result_text = _first_text(
            result,
            ("text", "summary", "message", "output", "content"),
        )
        if result_text:
            return result_text
        payload_text = _payloads_text(result.get("payloads"))
        if payload_text:
            return payload_text

    payload_text = _payloads_text(cli_json.get("payloads"))
    if payload_text:
        return payload_text

    raise OpenClawCLIError("openclaw agent JSON did not contain text output")


def _payloads_text(payloads: Any) -> str | None:
    if not isinstance(payloads, list):
        return None
    texts = [
        str(payload["text"]).strip()
        for payload in payloads
        if isinstance(payload, dict)
        and isinstance(payload.get("text"), str)
        and payload["text"].strip()
    ]
    return "\n\n".join(texts) if texts else None


def _first_text(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


__all__ = [
    "OpenClawCLIError",
    "extract_agent_text",
    "gateway_reachable",
    "require_gateway",
    "run_agent",
]
