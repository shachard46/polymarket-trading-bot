"""Each top-level package must import cleanly first in a fresh interpreter."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("module", ["agents_blueprint", "orchestrator"])
def test_fresh_interpreter_import(module):
    proc = subprocess.run([sys.executable, "-c", f"import {module}"], cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
