"""Open failure tags: registry, normalisation, p5 + p6 payloads."""

from __future__ import annotations

import json

import pytest

from obsidian_utils import ObsidianManager
from orchestrator import scraper
from tests.orchestrator.test_phase5_archive import _seed_resolved_market

from config.trading_constants import FAILURE_TAG_MIN_COUNT
from orchestrator import failure_tags as ft
from orchestrator import ledger, phases
from tests.orchestrator.test_learning_loop import Overseer


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.delenv("OVERSEER_MIN_NEW_RESOLVED", raising=False)
    v = ObsidianManager(vault_base=tmp_path)
    v.cold_start_protocol()
    return v


def test_normalize():
    n = ft.normalize_failure_class
    assert n("Variance", None, {}) == ("variance", None, None)
    assert n("new:stale_poll", "  poll was old ", {}) == ("new:stale_poll", "stale_poll", "poll was old")
    assert n("new:stale_poll", None, {})[0] == "unclassified"  # no definition
    assert n("new:Bad Slug!", "x", {})[0] == "unclassified"
    assert n("new:ab", "x", {})[0] == "unclassified"
    assert n("nonsense", "x", {})[0] == "unclassified"
    assert n(None, None, {})[0] == "unclassified"
    reg = {"stale_poll": {"definition": "d", "count": 1}}
    assert n("new:stale_poll", None, reg)[0] == "new:stale_poll"  # reuse w/o definition
    assert n("stale_poll", None, reg)[0] == "new:stale_poll"  # bare slug of known tag
    assert n("other_tag", "d", reg)[0] == "unclassified"  # bare unknown slug


def test_registry_counting_and_threshold(vault):
    assert ft.load_registry(vault) == {}
    for i in range(FAILURE_TAG_MIN_COUNT - 1):
        ft.record_tag(vault, "stale_poll", "first def" if i == 0 else "ignored")
    reg = ft.load_registry(vault)
    assert reg["stale_poll"]["count"] == FAILURE_TAG_MIN_COUNT - 1
    assert reg["stale_poll"]["definition"] == "first def"
    assert "stale_poll" in ft.candidates(reg) and not ft.established(reg)
    ft.record_tag(vault, "stale_poll", None)
    reg = ft.load_registry(vault)
    assert "stale_poll" in ft.established(reg) and not ft.candidates(reg)
    assert (vault._dirs["system"] / "failure_tags.json").exists()


def test_corrupt_registry_is_empty(vault):
    (vault._dirs["system"] / "failure_tags.json").write_text("{nope", encoding="utf-8")
    assert ft.load_registry(vault) == {}


def test_overseer_views_split():
    reg = {
        "est_tag": {"definition": "e", "count": FAILURE_TAG_MIN_COUNT},
        "cand_tag": {"definition": "c", "count": 1},
    }
    counts = {"variance": 2, "new:est_tag": 3, "new:cand_tag": 1}
    merged, cand = ft.overseer_failure_views(counts, reg)
    assert merged == {"variance": 2, "new:est_tag": 3}
    assert cand == {"new:cand_tag": {"count": 1, "definition": "c"}}


def test_p6_payload_has_established_and_candidates(vault, monkeypatch):
    monkeypatch.setenv("OVERSEER_MIN_NEW_RESOLVED", "0")
    for _ in range(FAILURE_TAG_MIN_COUNT):
        ft.record_tag(vault, "est_tag", "established def")
    ft.record_tag(vault, "cand_tag", "candidate def")
    ledger.record(vault, "a", "post_mortem", failure_class="new:est_tag")
    ledger.record(vault, "b", "post_mortem", failure_class="new:cand_tag")
    ledger.record(vault, "c", "post_mortem", failure_class="variance")
    run = Overseer()
    phases.phase6_macro_learning_loop(vault, runner=run)
    payload = run.payloads[0]
    assert payload["failure_class_counts"] == {"new:est_tag": 1, "variance": 1}
    assert payload["candidate_failure_tags"] == {"new:cand_tag": {"count": 1, "definition": "candidate def"}}


def _run_p5(vault, monkeypatch, mid, answer):
    _seed_resolved_market(vault, mid)
    monkeypatch.setattr(scraper, "fetch_resolution",
                        lambda m: {"market_id": m, "status": "closed", "outcome": "yes"})
    seen = []

    def runner(role, payload):
        seen.append(payload)
        return {"market_id": payload["market_id"], "post_mortem_analysis": "para", "error": None, **answer}

    phases.phase5_resolution_and_post_mortem(vault, runner=runner)
    return seen[0]


def _pm_class(vault, mid):
    return [r for r in ledger.iter_rows(vault) if r["market_id"] == mid and r["stage"] == "post_mortem"][-1]["failure_class"]


def test_p5_payload_and_new_tag_flow(vault, monkeypatch):
    for _ in range(FAILURE_TAG_MIN_COUNT):
        ft.record_tag(vault, "est_tag", "established def")
    ft.record_tag(vault, "cand_tag", "candidate def")
    payload = _run_p5(vault, monkeypatch, "0xaaa", {
        "failure_class": "new:fresh_tag", "failure_tag_definition": "a fresh cause"})
    assert "variance" in payload["failure_classes"]
    assert payload["established_failure_tags"] == {"new:est_tag": "established def"}
    assert payload["candidate_failure_tags"] == {"new:cand_tag": "candidate def"}
    assert _pm_class(vault, "0xaaa") == "new:fresh_tag"
    reg = ft.load_registry(vault)
    assert reg["fresh_tag"]["count"] == 1 and reg["fresh_tag"]["definition"] == "a fresh cause"
    assert vault.read_market_record("0xaaa", "post_mortem")["failure_class"] == "new:fresh_tag"


def test_p5_invalid_class_is_unclassified_and_not_flagged(vault, monkeypatch):
    _run_p5(vault, monkeypatch, "0xbbb", {"failure_class": "new:Bad Slug!", "failure_tag_definition": "x"})
    assert _pm_class(vault, "0xbbb") == "unclassified"
    assert ft.load_registry(vault) == {}
    assert not vault.post_mortem_path("0xbbb").read_text().startswith("---\nstatus: inactive")
    assert "inactive" not in vault.post_mortem_path("0xbbb").read_text().split("##")[0]


def test_p5_fixed_class_unchanged(vault, monkeypatch):
    _run_p5(vault, monkeypatch, "0xccc", {"failure_class": "bad_info"})
    assert _pm_class(vault, "0xccc") == "bad_info"
