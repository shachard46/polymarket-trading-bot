"""The suite guard in tests/conftest.py blocks real network and real sleeps."""

from __future__ import annotations

import socket
import time

import pytest


def test_non_loopback_connect_blocked():
    s = socket.socket()
    try:
        with pytest.raises(RuntimeError, match="network blocked in tests"):
            s.connect(("93.184.216.34", 80))
    finally:
        s.close()


def test_create_connection_blocked():
    with pytest.raises(RuntimeError, match="network blocked in tests"):
        socket.create_connection(("example.com", 80), timeout=1)


def test_loopback_allowed():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    try:
        cli = socket.socket()
        cli.settimeout(2)
        cli.connect(srv.getsockname())
        cli.close()
    finally:
        srv.close()


def test_long_sleep_blocked():
    with pytest.raises(RuntimeError, match="sleep blocked"):
        time.sleep(1)


def test_tiny_sleep_ok():
    time.sleep(0)
    time.sleep(0.01)


def test_explicit_monkeypatch_sleep_wins(monkeypatch):
    calls = []
    monkeypatch.setattr(time, "sleep", lambda s: calls.append(s))
    time.sleep(30)
    assert calls == [30]


@pytest.mark.live
def test_live_marker_exempt():
    assert socket.socket.connect.__name__ == "connect"
    assert "guarded" not in time.sleep.__name__
