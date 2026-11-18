"""Static checks for deploy/: shell syntax, systemd unit keys, env example coverage."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
SCRIPTS = sorted(DEPLOY.glob("*.sh"))

# Env vars read only by tests or by the platform, not by the app configuration.
IGNORE_ENV = {"PYTEST_CURRENT_TEST"}

_NAME = r"[A-Z][A-Z0-9_]{2,}"
_PATTERNS = [
    re.compile(r"\b\w*_ENV\s*=\s*[\"'](" + _NAME + r")[\"']"),
    re.compile(r"(?:environ(?:\.get|\.pop|\.setdefault)?|getenv|_env\w*)\(\s*[\"'](" + _NAME + r")[\"']"),
    re.compile(r"environ\[\s*[\"'](" + _NAME + r")[\"']"),
    re.compile(r"\bapi_key_env\s*=\s*[\"'](" + _NAME + r")[\"']"),
]


def _code_env_names() -> set[str]:
    names: set[str] = set()
    files = [ROOT / "main.py"]
    for sub in ("orchestrator", "config", "obsidian_utils"):
        files += (ROOT / sub).rglob("*.py")
    for path in files:
        text = path.read_text(encoding="utf-8")
        for pat in _PATTERNS:
            names.update(pat.findall(text))
    return names - IGNORE_ENV


def test_scripts_exist():
    assert {p.name for p in SCRIPTS} >= {"setup.sh", "backup.sh"}


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_bash_syntax(script):
    bash = shutil.which("bash")
    assert bash
    res = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_shellcheck(script):
    exe = shutil.which("shellcheck")
    if not exe:
        pytest.skip("shellcheck not installed")
    res = subprocess.run([exe, str(script)], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout


def test_scripts_strict_mode():
    for script in SCRIPTS:
        assert "set -euo pipefail" in script.read_text(encoding="utf-8"), script.name


def _unit_keys(path: Path) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    cur = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = sections.setdefault(line[1:-1], {})
        elif cur is not None and "=" in line:
            k, v = line.split("=", 1)
            cur[k.strip()] = v.strip()
    return sections


@pytest.mark.parametrize(
    "name,exec_part",
    [("pmbot-hub.service", "main.py"), ("pmbot-bot.service", "orchestrator.discord_bot")],
)
def test_long_running_units(name, exec_part):
    svc = _unit_keys(DEPLOY / name)["Service"]
    assert exec_part in svc["ExecStart"]
    assert ".venv/bin/python" in svc["ExecStart"]
    assert svc["User"] == "pmbot"
    assert svc["Restart"] == "on-failure"
    assert svc["EnvironmentFile"] == "/etc/pmbot/pmbot.env"
    assert svc["WorkingDirectory"]
    assert svc["NoNewPrivileges"] == "true"
    assert svc["ProtectSystem"] == "full"
    assert svc["ReadWritePaths"]


def test_backup_units():
    svc = _unit_keys(DEPLOY / "pmbot-backup.service")["Service"]
    assert svc["Type"] == "oneshot"
    assert svc["ExecStart"].endswith("backup.sh")
    assert svc["User"] == "pmbot"
    timer = _unit_keys(DEPLOY / "pmbot-backup.timer")
    assert "daily" in timer["Timer"]["OnCalendar"] or timer["Timer"]["OnCalendar"].startswith("*-*-*")
    assert "timers.target" in timer["Install"]["WantedBy"]


def test_env_example_covers_code():
    text = (DEPLOY / "pmbot.env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^#?\s*(" + _NAME + r")=", text, flags=re.M))
    missing = sorted(_code_env_names() - documented)
    assert not missing, f"env vars read in code but absent from deploy/pmbot.env.example: {missing}"


def test_env_example_mode_defaults_to_stub():
    text = (DEPLOY / "pmbot.env.example").read_text(encoding="utf-8")
    assert re.search(r"^PMBOT_MODE=stub$", text, flags=re.M)
