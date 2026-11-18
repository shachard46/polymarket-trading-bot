"""Concierge blueprint + Overseer directive approval (CONCIERGE_APPROVE_DIRECTIVES)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from config.trading_constants import FILTERS
from obsidian_utils import ObsidianManager
from orchestrator import notify, phases
from orchestrator.phases import p4_approval
from orchestrator.phases.p6_overseer import process_directive_votes
from tests.orchestrator.test_learning_loop import add_resolutions, doc


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    for k in ("CONCIERGE_APPROVE_DIRECTIVES", "CONCIERGE_DIRECTIVE_TTL_SEC", "CONCIERGE_APPROVAL",
              "OVERSEER_MIN_NEW_RESOLVED", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    v = ObsidianManager(vault_base=tmp_path)
    v.cold_start_protocol()
    return v


def _proposal_md() -> str:
    knob = "research_max_q_gap"
    return doc(**{knob: round(FILTERS[knob] * 1.1, 4)})


def _runner(md: str):
    def run(role, payload, *a, **k):
        assert role == "overseer"
        return json.dumps({"new_directives_markdown": md, "rationale": "because", "error": None})
    return run


def _run_p6(vault, md=None):
    add_resolutions(vault, 30)
    phases.phase6_macro_learning_loop(vault, runner=_runner(md or _proposal_md()))


def _vote(vault, pid: str, vote: str, key="proposal_id"):
    (notify.inbox_dir(vault) / f"vote-{pid}.json").write_text(json.dumps({key: pid, "vote": vote}))


def test_flag_off_applies_as_before(vault):
    _run_p6(vault)
    assert vault.latest_directives_version() == 2
    assert not list(notify.outbox_dir(vault).glob("directive-*.md"))


def test_flag_on_writes_proposal_without_applying(vault, monkeypatch):
    monkeypatch.setenv("CONCIERGE_APPROVE_DIRECTIVES", "1")
    _run_p6(vault)
    assert vault.latest_directives_version() == 1
    path = notify.outbox_dir(vault) / "directive-v2.md"
    text = path.read_text()
    for needle in ("proposal_id: directive-v2", "expires_at:", "research_max_q_gap:", "->", "because"):
        assert needle in text


def test_flag_on_invalid_proposal_writes_nothing(vault, monkeypatch):
    monkeypatch.setenv("CONCIERGE_APPROVE_DIRECTIVES", "1")
    _run_p6(vault, doc(research_max_q_gap=FILTERS["research_max_q_gap"] * 5))
    assert not list(notify.outbox_dir(vault).glob("directive-*.md"))


def test_yes_applies(vault, monkeypatch):
    monkeypatch.setenv("CONCIERGE_APPROVE_DIRECTIVES", "1")
    _run_p6(vault)
    _vote(vault, "directive-v2", "yes")
    assert process_directive_votes(vault)["applied"] == 1
    assert vault.latest_directives_version() == 2
    assert not (notify.outbox_dir(vault) / "directive-v2.md").exists()


def test_yes_after_version_moved_rejected(vault, monkeypatch):
    monkeypatch.setenv("CONCIERGE_APPROVE_DIRECTIVES", "1")
    _run_p6(vault)
    vault.write_directives({"new_directives_markdown": doc(body="Other."), "rationale": "r"})
    assert vault.latest_directives_version() == 2
    _vote(vault, "directive-v2", "yes")
    assert process_directive_votes(vault)["rejected"] == 1
    assert vault.latest_directives_version() == 2


def test_no_and_expired_not_applied(vault, monkeypatch):
    monkeypatch.setenv("CONCIERGE_APPROVE_DIRECTIVES", "1")
    _run_p6(vault)
    _vote(vault, "directive-v2", "no")
    assert process_directive_votes(vault)["declined"] == 1
    assert vault.latest_directives_version() == 1


def test_yes_after_expiry_not_applied(vault, monkeypatch):
    monkeypatch.setenv("CONCIERGE_APPROVE_DIRECTIVES", "1")
    _run_p6(vault)
    _vote(vault, "directive-v2", "yes")
    later = datetime.now(timezone.utc) + timedelta(days=3)
    assert process_directive_votes(vault, now=later)["expired"] == 1
    assert vault.latest_directives_version() == 1


def test_unvoted_proposal_expires(vault, monkeypatch):
    monkeypatch.setenv("CONCIERGE_APPROVE_DIRECTIVES", "1")
    _run_p6(vault)
    later = datetime.now(timezone.utc) + timedelta(days=3)
    assert process_directive_votes(vault, now=later)["expired"] == 1
    assert not (notify.outbox_dir(vault) / "directive-v2.md").exists()


def test_offer_and_directive_votes_do_not_collide(vault, monkeypatch):
    monkeypatch.setenv("CONCIERGE_APPROVE_DIRECTIVES", "1")
    _run_p6(vault)
    _vote(vault, "directive-v2", "yes")
    offer_vote = notify.inbox_dir(vault) / "vote-0xabc-T1.json"
    offer_vote.write_text(json.dumps({"offer_id": "0xabc-T1", "vote": "yes"}))
    p4_approval.process_inbox(vault)
    assert (notify.inbox_dir(vault) / "vote-directive-v2.json").exists()  # untouched by offers
    assert not offer_vote.exists()  # offers processor handled (ignored: unknown offer)
    summary = process_directive_votes(vault)
    assert summary["applied"] == 1
    assert (notify.inbox_dir(vault) / "_done" / "vote-0xabc-T1.json").exists()


def test_concierge_blueprint_loads():
    from agents_blueprint import AGENTS

    spec = AGENTS["concierge"]
    assert spec["openclaw"]["openclaw_agent_id"] == "polymarket-concierge"
    assert spec["hub_spawned"] is False
    assert "never vote on your own" in spec["system_prompt"].lower()
    assert all(AGENTS[r]["hub_spawned"] for r in AGENTS if r != "concierge")
