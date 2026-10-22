"""Poll-loop behaviour tests for execute_aiq_query (requests mocked)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "execute_aiq_query",
    _ROOT / "skills/execute-aiq-query/execute_aiq_query.py",
)
assert _SPEC and _SPEC.loader
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)


def _resp(body=None, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}", response=r)
    return r


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setenv("AIQ_POLL_INTERVAL_SEC", "0.01")
    monkeypatch.setenv("AIQ_TIMEOUT_SEC", "5")
    monkeypatch.setattr(_mod.requests, "post", lambda *a, **k: _resp({"job_id": "j1"}))


def _install_get(monkeypatch, responses):
    seq = list(responses)

    def fake_get(url, timeout=None):
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(_mod.requests, "get", fake_get)


def test_lowercase_success(monkeypatch):
    _install_get(monkeypatch, [_resp({"status": "success"}), _resp({"report": "R"})])
    out = _mod.execute_aiq_query("q")
    assert out.error is None and out.research_data == "R"


def test_null_status_keeps_polling(monkeypatch):
    _install_get(
        monkeypatch,
        [_resp({"status": None}), _resp({"status": "completed"}), _resp({"report": "R"})],
    )
    assert _mod.execute_aiq_query("q").research_data == "R"


def test_not_found_is_terminal(monkeypatch):
    _install_get(monkeypatch, [_resp({"status": "not_found"})])
    out = _mod.execute_aiq_query("q")
    assert out.error and "not_found" in out.error


def test_transient_503_then_success(monkeypatch):
    _install_get(
        monkeypatch,
        [
            _resp(None, 503),
            requests.ConnectionError("boom"),
            _resp({"status": "success"}),
            _resp({"report": "R"}),
        ],
    )
    out = _mod.execute_aiq_query("q")
    assert out.error is None and out.research_data == "R"


def test_4xx_fails(monkeypatch):
    _install_get(monkeypatch, [_resp(None, 404)])
    out = _mod.execute_aiq_query("q")
    assert out.error and "404" in out.error


@pytest.mark.parametrize("report", [None, "", "   ", 5])
def test_empty_report_is_error(monkeypatch, report):
    _install_get(monkeypatch, [_resp({"status": "success"}), _resp({"report": report})])
    out = _mod.execute_aiq_query("q")
    assert out.error and out.research_data == ""


def test_deadline_enforced(monkeypatch):
    monkeypatch.setenv("AIQ_TIMEOUT_SEC", "0.2")
    timeouts = []

    def fake_get(url, timeout=None):
        timeouts.append(timeout)
        return _resp({"status": "running"})

    monkeypatch.setattr(_mod.requests, "get", fake_get)
    out = _mod.execute_aiq_query("q")
    assert out.error and "did not complete" in out.error
    assert all(t <= 0.2 for t in timeouts)
