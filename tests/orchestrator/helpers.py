"""Shared helpers for orchestrator tests."""

from __future__ import annotations

from orchestrator import ledger


def _ids(pred, n):
    """First n synthetic market ids (s0, s1, ...) for which pred(id) is true."""
    return [m for m in (f"s{i}" for i in range(4000)) if pred(m)][:n]


def _rows(vault, stage):
    """Ledger rows of one stage."""
    return [r for r in ledger.iter_rows(vault) if r["stage"] == stage]
