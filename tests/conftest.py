"""Repo-wide test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _tmp_vault_env(monkeypatch, tmp_path_factory):
    """Default vault base is a tmp dir, so code that resolves it (e.g. LLM usage rows) never writes into the repo."""
    monkeypatch.setenv("PMBOT_VAULT_PATH", str(tmp_path_factory.mktemp("vault_env")))


@pytest.fixture(autouse=True)
def _default_two_judges(monkeypatch):
    """Tests assume the two-judge arm unless they pick another (the shipped default is the per-market A/B)."""
    monkeypatch.setenv("JUDGE_ARM_MODE", "two")


@pytest.fixture(autouse=True)
def _no_source_cache():
    """The classifier cache is bound per pipeline run; never let one test's file leak into the next."""
    from orchestrator import source_quality

    source_quality.bind_cache(None)
    yield
    source_quality.bind_cache(None)


def pytest_configure(config):
    config.addinivalue_line("markers", "live: opt-in test that may use the real network or real sleeps; exempt from suite guards")


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}


def _is_local(address) -> bool:
    if isinstance(address, (str, bytes)):
        return True  # AF_UNIX path
    try:
        host = address[0]
    except (TypeError, IndexError):
        return True
    if isinstance(host, bytes):
        host = host.decode(errors="ignore")
    return host in _LOOPBACK_HOSTS or str(host).startswith("127.")


@pytest.fixture(autouse=True)
def _block_network_and_sleep(request, monkeypatch):
    """No test may reach a non-loopback host or really sleep. Mark a test `live` to opt out.

    Autouse fixtures run before the test body, so a test's own monkeypatch of
    time.sleep or socket functions is applied afterwards and wins.
    """
    if request.node.get_closest_marker("live"):
        yield
        return
    import socket
    import time

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create = socket.create_connection
    real_sleep = time.sleep

    def guarded_connect(self, address):
        if not _is_local(address):
            raise RuntimeError(f"network blocked in tests: {address}")
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        if not _is_local(address):
            raise RuntimeError(f"network blocked in tests: {address}")
        return real_connect_ex(self, address)

    def guarded_create(address, *args, **kwargs):
        if not _is_local(address):
            raise RuntimeError(f"network blocked in tests: {address}")
        return real_create(address, *args, **kwargs)

    def guarded_sleep(seconds=0):
        if seconds > 0.05:
            raise RuntimeError(f"real sleep blocked in tests: time.sleep({seconds})")
        return real_sleep(seconds)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", guarded_create)
    monkeypatch.setattr(time, "sleep", guarded_sleep)
    yield
