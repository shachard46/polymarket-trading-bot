"""Pure helpers of the built-in Discord bot (discord.py not needed)."""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timezone

import pytest

from obsidian_utils import ObsidianManager
from orchestrator import discord_bot as db
from orchestrator import ledger, notify

OWNER = 111


@pytest.fixture()
def vault(tmp_path):
    v = ObsidianManager(vault_base=tmp_path)
    v.cold_start_protocol()
    return v


OFFER = {
    "offer_id": "0xabc-T1", "market_id": "0xabc", "mode": "approval", "question": "Will X?", "url": "https://p/x",
    "side": "YES", "p": 0.6, "p_low": 0.5, "p_high": 0.7, "q_entry": 0.4, "edge": 0.1, "allocation_usd": 20,
    "shares": 50, "ask": 0.41, "manipulation_risk": "low", "integ_score": 0.1, "integ_flags": [],
    "evidence_summary": "stuff", "expires_at": "2026-05-01T12:00:00+00:00",
}


def test_render_offer_fields_and_na():
    text = db.render_offer(OFFER)
    for s in ("BUY OFFER 0xabc-T1", "Will X?", "https://p/x", "p: 0.5-0.7 (point 0.6) vs q: 0.4", "edge: 0.1", "buttons"):
        assert s in text
    assert "n/a" in db.render_offer({"offer_id": "o", "mode": "informational"})
    assert "buttons" not in db.render_offer({**OFFER, "mode": "informational"})
    assert len(db.render_offer({**OFFER, "evidence_summary": "x" * 5000})) <= db.MAX_MSG


def test_render_digest():
    text = db.render_digest({"date": "2026-05-01", "realised_pnl_after_tax": 1.5, "integrity_rejects": {"ramp_thin": 2}})
    assert "2026-05-01" in text and "ramp_thin=2" in text and "n/a" in text


def test_render_directive_and_parse():
    md = "---\nproposal_id: directive-v3\nexpires_at: 2026-05-03\n---\n\n# Proposal\nchange x\n"
    text = db.render_directive(md)
    assert "directive-v3" in text and "change x" in text and "2026-05-03" in text
    assert db.parse_directive(md)[0]["proposal_id"] == "directive-v3"


def test_pending_posts_and_posted_marker(tmp_path):
    out = tmp_path
    for n in ("offer-a.json", "digest-2026-05-01.json", "directive-v1.md", "other.txt", "_posted.json"):
        (out / n).write_text("{}")
    (out / "_done").mkdir()
    (out / "_done" / "offer-old.json").write_text("{}")
    names = [p.name for p in db.pending_posts(out, set())]
    assert names == ["digest-2026-05-01.json", "directive-v1.md", "offer-a.json"]
    assert [p.name for p in db.pending_posts(out, {"offer-a.json"})] == ["digest-2026-05-01.json", "directive-v1.md"]
    db.save_posted(out, {"offer-a.json"})
    assert json.loads((out / "_posted.json").read_text()) == ["offer-a.json"]
    assert db.load_posted(out) == {"offer-a.json"}


def test_item_id_and_needs_vote(tmp_path):
    p = tmp_path / "offer-x.json"
    assert db.item_id(p, {"offer_id": "real"}) == "real"
    assert db.item_id(p, None) == "x"
    assert db.item_id(tmp_path / "directive-v3.md") == "directive-v3"
    assert db.needs_vote(p, {"mode": "approval"}) and not db.needs_vote(p, {"mode": "informational"})
    assert db.needs_vote(tmp_path / "directive-v3.md") and not db.needs_vote(tmp_path / "digest-d.json")


def test_write_vote_owner_only_and_formats(tmp_path):
    assert db.write_vote(tmp_path, "0xabc-T1", "yes", 999, OWNER) is None
    assert list(tmp_path.iterdir()) == []
    path = db.write_vote(tmp_path, "0xabc-T1", "YES", OWNER, OWNER)
    assert json.loads(path.read_text()) == {"offer_id": "0xabc-T1", "vote": "yes"}
    assert db.write_vote(tmp_path, "0xabc-T1", "no", OWNER, OWNER) is None  # no overwrite
    assert json.loads(path.read_text())["vote"] == "yes"
    d = db.write_vote(tmp_path, "directive-v3", "no", OWNER, OWNER)
    assert d.name == "vote-directive-v3.json"
    assert json.loads(d.read_text()) == {"proposal_id": "directive-v3", "vote": "no"}
    assert db.write_vote(tmp_path, "x", "maybe", OWNER, OWNER) is None
    assert db.write_vote(tmp_path, "../evil", "yes", OWNER, OWNER) is None


def test_vote_is_read_by_hub(vault):
    from orchestrator.phases import p6_overseer  # noqa: F401  (vote id convention)

    inbox = notify.inbox_dir(vault)
    db.write_vote(inbox, "directive-v2", "yes", OWNER, OWNER)
    vote = notify.read_json(inbox / "vote-directive-v2.json")
    assert vote["vote"] == "yes" and (inbox / "vote-directive-v2.json").stem.startswith("vote-" + notify.DIRECTIVE_ID_PREFIX)


def test_is_expired():
    now = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)
    assert db.is_expired("2026-05-01T11:00:00+00:00", now)
    assert not db.is_expired("2026-05-01T13:00:00+00:00", now)
    assert not db.is_expired(None, now) and not db.is_expired("garbage", now)


def test_in_quiet_hours():
    at = lambda h: datetime(2026, 5, 1, h, 30, tzinfo=timezone.utc)  # noqa: E731
    assert db.in_quiet_hours("23-07", at(23)) and db.in_quiet_hours("23-07", at(3))
    assert not db.in_quiet_hours("23-07", at(7)) and not db.in_quiet_hours("23-07", at(12))
    assert db.in_quiet_hours("1-5", at(2)) and not db.in_quiet_hours("1-5", at(5))
    assert not db.in_quiet_hours(None, at(3)) and not db.in_quiet_hours("bogus", at(3))
    # 12:30 UTC is 21:30 JST: not quiet; 15:30 UTC is 00:30 JST: quiet
    assert not db.in_quiet_hours("23-07", at(12), tz="Asia/Tokyo")
    assert db.in_quiet_hours("23-07", at(15), tz="Asia/Tokyo")


def test_build_qa_context_includes_and_caps(vault):
    (vault._dirs["system"] / "stats.md").write_text("# stats\nBrier 0.2\n", encoding="utf-8")
    ledger.record(vault, "0xabcdef12", "traded", q=0.3, p=0.6, price=0.32, size_usd=10, side="YES", shares=31.0)
    ctx = db.build_qa_context(vault, "how is 0xabcdef12 doing?")
    assert "Brier 0.2" in ctx and "0xabcdef12" in ctx
    (vault._dirs["system"] / "stats.md").write_text("z" * 50000, encoding="utf-8")
    capped = db.build_qa_context(vault, "q", max_chars=1000)
    assert len(capped) <= 1000 + 40 and capped.endswith("[context truncated]")


def test_answer_question_uses_complete_mocked(vault, monkeypatch):
    calls = {}

    def fake_complete(**kw):
        calls.update(kw)
        return "Brier is 0.2 (stats.md)"

    mod = types.ModuleType("orchestrator.direct_runner")
    mod.complete = fake_complete
    monkeypatch.setitem(sys.modules, "orchestrator.direct_runner", mod)
    import orchestrator

    monkeypatch.setattr(orchestrator, "direct_runner", mod, raising=False)
    assert db.answer_question(vault, "what is brier?") == "Brier is 0.2 (stats.md)"
    assert calls["role"] == "concierge" and "Question: what is brier?" in calls["user_text"] and calls["system_prompt"]


def test_qa_system_prompt_section_and_fallback(tmp_path):
    f = tmp_path / "AGENTS.md"
    f.write_text("# x\n## Q&A mode\nonly quote numbers\n## Other\nz\n", encoding="utf-8")
    assert db.qa_system_prompt(f) == "only quote numbers"
    assert db.qa_system_prompt(tmp_path / "missing.md") == db._FALLBACK_QA_PROMPT
    assert "read-only" in db.qa_system_prompt()  # real blueprint section loads


def test_is_owner():
    assert db.is_owner("111", 111) and not db.is_owner(1, 111) and not db.is_owner(None, 111)


def test_vote_view_real_discord(tmp_path):
    discord = pytest.importorskip("discord")
    import asyncio

    async def build():  # View.__init__ needs a running loop
        return db.build_vote_view(discord, "0xabc-T1", tmp_path, OWNER)

    view = asyncio.run(build())
    assert view.timeout is None and view.is_persistent()
    ids = [c.custom_id for c in view.children]
    assert ids == ["vote:yes:0xabc-T1", "vote:no:0xabc-T1"] and all(len(i) <= 100 for i in ids)
    assert not any(c.disabled for c in view.children)

    async def build_done():
        return db.build_vote_view(discord, "directive-v3", tmp_path, OWNER, disabled=True)

    assert all(c.disabled for c in asyncio.run(build_done()).children)
