"""CLI regression tests for execute-aiq-query/run.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SKILL_DIR = _ROOT / "skills/execute-aiq-query"
_RUN_PY = _SKILL_DIR / "run.py"
_OPENCLAW_CONFIG = Path("/home/boldplane/.openclaw/config/trading_constants.py")
_REPO_CONFIG = _ROOT / "config/trading_constants.py"


def _run_py_env() -> dict[str, str]:
    """Prefer OpenClaw gateway layout; fall back to repo config for local CI."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    if _OPENCLAW_CONFIG.is_file():
        return env
    if _REPO_CONFIG.is_file():
        env["PYTHONPATH"] = str(_ROOT)
    return env


def _can_import_skill_module() -> bool:
    return _OPENCLAW_CONFIG.is_file() or _REPO_CONFIG.is_file()


def test_run_py_usage_exits_2():
    result = subprocess.run(
        [sys.executable, str(_RUN_PY)],
        cwd=_SKILL_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr


@pytest.mark.skipif(not _can_import_skill_module(), reason="no config module on PYTHONPATH")
def test_run_py_invalid_json_exits_1():
    result = subprocess.run(
        [sys.executable, str(_RUN_PY), "not-json"],
        cwd=_SKILL_DIR,
        env=_run_py_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "invalid input:" in result.stderr


@pytest.mark.skipif(not _can_import_skill_module(), reason="no config module on PYTHONPATH")
def test_run_py_missing_query_exits_1():
    result = subprocess.run(
        [sys.executable, str(_RUN_PY), "{}"],
        cwd=_SKILL_DIR,
        env=_run_py_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "invalid input:" in result.stderr


@pytest.mark.skipif(
    not _OPENCLAW_CONFIG.is_file(),
    reason="OpenClaw gateway config not present (run on boldplane)",
)
def test_run_py_imports_without_pythonpath():
    """run.py must import config from hardcoded OpenClaw root without PYTHONPATH."""
    run_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, str(_RUN_PY), '{"query": "test question"}'],
        cwd=_SKILL_DIR,
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = result.stdout + result.stderr
    assert "ModuleNotFoundError" not in combined
    assert "No module named 'config'" not in combined
    assert result.returncode in (0, 1)


@pytest.mark.skipif(not _REPO_CONFIG.is_file(), reason="repo config missing")
def test_run_py_returns_json_with_mocked_aiq():
    """Smoke: CLI prints ExecuteAiqQueryOutput JSON when A-IQ is mocked."""
    env = _run_py_env()
    env["PYTHONPATH"] = str(_SKILL_DIR) + os.pathsep + str(_ROOT)

    mock_script = f"""
import json
import sys
sys.path.insert(0, {str(_SKILL_DIR)!r})
sys.path.insert(0, {str(_ROOT)!r})
from execute_aiq_query import ExecuteAiqQueryOutput
import run as run_mod
from unittest.mock import patch

with patch(
    "execute_aiq_query.execute_aiq_query",
    return_value=ExecuteAiqQueryOutput(research_data="report", error=None),
):
    sys.argv = ["run.py", '{{"query": "Will X happen by 2026?"}}']
    run_mod.main()
"""
    result = subprocess.run(
        [sys.executable, "-c", mock_script],
        cwd=_SKILL_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert body == {"research_data": "report", "error": None}
