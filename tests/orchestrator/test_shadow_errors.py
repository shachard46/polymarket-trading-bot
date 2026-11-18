"""Shadow FULL run errors: ledger shadow_failed, never flag the real market inactive."""

from __future__ import annotations

import pytest

from orchestrator import evidence, phases
from tests.orchestrator.helpers import _ids, _rows
from tests.orchestrator.test_research_redesign import World, _seed


@pytest.fixture()
def world(monkeypatch, vault):
    monkeypatch.setenv("OPENCLAW_EVIDENCE_SOURCE", "aiq")
    return World(monkeypatch, vault)


def _flat_quick_far_full(payload):
    return (0.5, 0.42, 0.58) if payload.get("mode") == "quick" else (0.7, 0.62, 0.78)


def _shadow_market(world, vault):
    (mid,) = _ids(evidence.is_shadow, 1)
    world.market(mid, 0.5)
    _seed(vault, mid)
    world.dr_p = _flat_quick_far_full
    return mid


def _assert_failed_not_flagged(vault, mid, world):
    assert phases.phase3_qualitative_pipeline(vault, runner=world.runner) == []
    (row,) = _rows(vault, "shadow_failed")
    assert row["market_id"] == mid and row["reason"] and row["shadow"] is True
    assert not _rows(vault, "shadow_research")
    assert not vault.is_market_inactive(mid, dir_key="filters")
    assert not vault.is_market_inactive(mid)
    assert vault.read_active_research(mid) is None


def test_shadow_briefer_error(world, vault):
    mid = _shadow_market(world, vault)
    orig = world.runner

    def runner(role, payload):
        if role == "briefer":
            return {"market_id": mid, "research_queries": [], "error": "boom"}
        return orig(role, payload)

    world.runner = runner
    _assert_failed_not_flagged(vault, mid, world)
    assert "briefer" in _rows(vault, "shadow_failed")[0]["reason"]


def test_shadow_deep_researcher_bad_markdown(world, vault):
    mid = _shadow_market(world, vault)
    orig = world.runner

    def runner(role, payload):
        out = orig(role, payload)
        if role == "deep_researcher" and payload.get("mode") == "full":
            out["markdown"] = "not frontmatter"
        return out

    world.runner = runner
    _assert_failed_not_flagged(vault, mid, world)


def test_shadow_unexpected_exception(world, vault):
    mid = _shadow_market(world, vault)
    orig = world.runner

    def runner(role, payload):
        if role == "briefer":
            raise RuntimeError("weird")
        return orig(role, payload)

    world.runner = runner
    _assert_failed_not_flagged(vault, mid, world)


def test_shadow_infra_error_propagates(world, vault):
    from orchestrator.state import InfraUnavailable

    mid = _shadow_market(world, vault)

    orig = world.runner

    def runner(role, payload):
        if role == "briefer":
            raise InfraUnavailable("down")
        return orig(role, payload)

    with pytest.raises(InfraUnavailable):
        phases.phase3_qualitative_pipeline(vault, runner=runner)
    assert not _rows(vault, "shadow_failed")
