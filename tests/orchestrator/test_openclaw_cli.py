from __future__ import annotations

import os
import subprocess

import pytest

from orchestrator import openclaw_cli


@pytest.fixture(autouse=True)
def _reset_openclaw_cli_cache():
    openclaw_cli.reset_agent_cli_capability_cache()
    yield
    openclaw_cli.reset_agent_cli_capability_cache()


def _patch_openclaw_run(monkeypatch, *, help_text: str):
    calls = {}

    def fake_run(cmd, capture_output, text, timeout, check):
        calls["cmd"] = cmd
        calls["capture_output"] = capture_output
        calls["text"] = text
        calls["timeout"] = timeout
        calls["check"] = check
        if cmd[1:3] == ["agent", "--help"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=help_text,
                stderr="",
            )
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='{"payloads":[{"text":"ok"}]}',
            stderr="",
        )

    monkeypatch.setattr(openclaw_cli.shutil, "which", lambda binary: "/usr/bin/openclaw")
    monkeypatch.setattr(openclaw_cli.subprocess, "run", fake_run)
    return calls


def test_run_agent_uses_session_key_when_cli_supports_it(monkeypatch):
    calls = _patch_openclaw_run(
        monkeypatch,
        help_text="--session-key <key>\n--session-id <id>\n",
    )

    result = openclaw_cli.run_agent(
        "polymarket-evaluator",
        "prompt",
        session_key="agent:polymarket-evaluator:orch-0xabc",
        timeout=20,
    )

    assert result == {"payloads": [{"text": "ok"}]}
    assert calls["cmd"] == [
        "/usr/bin/openclaw",
        "agent",
        "--agent",
        "polymarket-evaluator",
        "--session-key",
        "orch-0xabc",
        "--message",
        "prompt",
        "--json",
        "--timeout",
        "20",
    ]
    assert calls["timeout"] == 50
    assert calls["capture_output"] is True
    assert calls["text"] is True
    assert calls["check"] is False


def test_run_agent_uses_session_id_without_agent_on_legacy_cli(monkeypatch):
    calls = _patch_openclaw_run(monkeypatch, help_text="--session-id <id>\n")

    result = openclaw_cli.run_agent(
        "polymarket-evaluator",
        "prompt",
        session_key="agent:polymarket-evaluator:orch-0xabc",
        timeout=20,
    )

    assert result == {"payloads": [{"text": "ok"}]}
    assert calls["cmd"] == [
        "/usr/bin/openclaw",
        "agent",
        "--session-id",
        "agent:polymarket-evaluator:orch-0xabc",
        "--message",
        "prompt",
        "--json",
        "--timeout",
        "20",
    ]


def test_run_agent_reports_nonzero_exit(monkeypatch):
    monkeypatch.setattr(openclaw_cli.shutil, "which", lambda binary: "/usr/bin/openclaw")
    monkeypatch.setattr(openclaw_cli, "openclaw_agent_max_attempts", lambda: 1)
    monkeypatch.setattr(
        openclaw_cli.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            2,
            stdout="",
            stderr="agent missing",
        ),
    )

    with pytest.raises(openclaw_cli.OpenClawCLIError, match="agent missing"):
        openclaw_cli.run_agent("missing", "prompt", session_key="agent:missing:orch-x")


def test_run_agent_reports_non_json_stdout(monkeypatch):
    monkeypatch.setattr(openclaw_cli.shutil, "which", lambda binary: "/usr/bin/openclaw")
    monkeypatch.setattr(openclaw_cli, "openclaw_agent_max_attempts", lambda: 1)
    monkeypatch.setattr(
        openclaw_cli.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout="not json",
            stderr="",
        ),
    )

    with pytest.raises(openclaw_cli.OpenClawCLIError, match="non-JSON"):
        openclaw_cli.run_agent("agent", "prompt", session_key="agent:agent:orch-x")


def test_run_agent_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(openclaw_cli.shutil, "which", lambda binary: "/usr/bin/openclaw")
    monkeypatch.setattr(openclaw_cli, "openclaw_agent_max_attempts", lambda: 3)
    monkeypatch.setattr(openclaw_cli, "openclaw_agent_retry_backoff", lambda: 0.0)
    monkeypatch.setattr(openclaw_cli.time, "sleep", lambda _seconds: None)

    attempts = {"count": 0}

    def fake_run(cmd, capture_output, text, timeout, check):
        if cmd[1:3] == ["agent", "--help"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="--session-id\n", stderr="")
        attempts["count"] += 1
        if attempts["count"] < 3:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="transient")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='{"payloads":[{"text":"ok"}]}',
            stderr="",
        )

    monkeypatch.setattr(openclaw_cli.subprocess, "run", fake_run)

    result = openclaw_cli.run_agent("agent", "prompt", session_key="agent:agent:orch-x")

    assert result == {"payloads": [{"text": "ok"}]}
    assert attempts["count"] == 3


def test_run_agent_exhausts_retries(monkeypatch):
    monkeypatch.setattr(openclaw_cli.shutil, "which", lambda binary: "/usr/bin/openclaw")
    monkeypatch.setattr(openclaw_cli, "openclaw_agent_max_attempts", lambda: 3)
    monkeypatch.setattr(openclaw_cli, "openclaw_agent_retry_backoff", lambda: 0.0)
    monkeypatch.setattr(openclaw_cli.time, "sleep", lambda _seconds: None)

    attempts = {"count": 0}

    def fake_run(cmd, capture_output, text, timeout, check):
        if cmd[1:3] == ["agent", "--help"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="--session-id\n", stderr="")
        attempts["count"] += 1
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="still failing")

    monkeypatch.setattr(openclaw_cli.subprocess, "run", fake_run)

    with pytest.raises(openclaw_cli.OpenClawCLIError, match="still failing"):
        openclaw_cli.run_agent("agent", "prompt", session_key="agent:agent:orch-x")

    assert attempts["count"] == 3


def test_run_agent_retries_on_timeout(monkeypatch):
    monkeypatch.setattr(openclaw_cli.shutil, "which", lambda binary: "/usr/bin/openclaw")
    monkeypatch.setattr(openclaw_cli, "openclaw_agent_max_attempts", lambda: 2)
    monkeypatch.setattr(openclaw_cli, "openclaw_agent_retry_backoff", lambda: 0.0)
    monkeypatch.setattr(openclaw_cli.time, "sleep", lambda _seconds: None)

    attempts = {"count": 0}

    def fake_run(cmd, capture_output, text, timeout, check):
        if cmd[1:3] == ["agent", "--help"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="--session-id\n", stderr="")
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='{"payloads":[{"text":"ok"}]}',
            stderr="",
        )

    monkeypatch.setattr(openclaw_cli.subprocess, "run", fake_run)

    result = openclaw_cli.run_agent("agent", "prompt", session_key="agent:agent:orch-x")

    assert result == {"payloads": [{"text": "ok"}]}
    assert attempts["count"] == 2


def test_gateway_reachable_success(monkeypatch):
    monkeypatch.setattr(openclaw_cli.shutil, "which", lambda binary: "/usr/bin/openclaw")
    monkeypatch.setattr(
        openclaw_cli.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="{}", stderr=""),
    )

    assert openclaw_cli.gateway_reachable() == (True, None)


def test_gateway_reachable_missing_binary(monkeypatch):
    monkeypatch.setattr(openclaw_cli.shutil, "which", lambda binary: None)

    ok, detail = openclaw_cli.gateway_reachable()

    assert ok is False
    assert "not found" in str(detail)


def test_extract_agent_text_from_payloads():
    text = openclaw_cli.extract_agent_text(
        {"payloads": [{"text": "first"}, {"text": "second"}]}
    )

    assert text == "first\n\nsecond"


def test_extract_agent_text_from_gateway_result_payloads():
    text = openclaw_cli.extract_agent_text(
        {"result": {"payloads": [{"text": "gateway text"}]}}
    )

    assert text == "gateway text"


def test_extract_agent_text_from_embedded_summary():
    text = openclaw_cli.extract_agent_text(
        {"meta": {"transport": "embedded"}, "summary": "embedded text"}
    )

    assert text == "embedded text"


def test_extract_agent_text_rejects_empty_payload():
    with pytest.raises(openclaw_cli.OpenClawCLIError, match="did not contain text"):
        openclaw_cli.extract_agent_text({"payloads": []})


def test_live_openclaw_agent_smoke():
    if os.environ.get("OPENCLAW_LIVE_SMOKE") != "1":
        pytest.skip("set OPENCLAW_LIVE_SMOKE=1 to run against a local Gateway")

    openclaw_cli.require_gateway()
    result = openclaw_cli.run_agent(
        "polymarket-evaluator",
        "Reply with exactly: pong",
        session_key="agent:polymarket-evaluator:orch-smoke",
        timeout=30,
    )

    assert openclaw_cli.extract_agent_text(result).strip()
