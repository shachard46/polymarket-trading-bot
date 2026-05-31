"""CLI regression tests for evaluate-market-metrics/run.py."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SKILL_DIR = _ROOT / "skills/evaluate-market-metrics"
_RUN_PY = _SKILL_DIR / "run.py"
_OPENCLAW_CONFIG = Path("/home/boldplane/.openclaw/config/trading_constants.py")


@pytest.mark.skipif(
    not _OPENCLAW_CONFIG.is_file(),
    reason="OpenClaw gateway config not present (run on boldplane)",
)
def test_run_py_imports_without_pythonpath():
    """run.py must import config from hardcoded OpenClaw root without PYTHONPATH."""
    run_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, str(_RUN_PY), '{"market_id": "0xtest"}'],
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
