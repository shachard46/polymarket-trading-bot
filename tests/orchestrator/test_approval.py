"""Concierge approval flag: offers, votes, expiry, Phase 5 skipping, stats section."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from obsidian_utils import ObsidianManager
from orchestrator import ledger, notify, phases, polymarket_api, scraper, stats
from orchestrator.phases import p4_execution

DATA = {"yes_price": 0.3, "volume": 10_000.0, "liquidity": 20_000.0, "days_to_resolution": 10,
        "question": "Will X happen?"}
MID = "0xabc1"


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    monkeypatch.delenv("CONCIERGE_APPROVAL", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    v = ObsidianManager(vault_base=tmp_path)
    v.cold_start_protocol()
    return v


def quotes(monkeypatch, yes=0.32, no=0.72):
    def fake(market_id, side):
        return {"ask": yes if side == "YES" else no, "depth_usd_within_2c": 1e9, "spread": 0.02, "token_id": side}

    monkeypatch.setattr(polymarket_api, "executable_quote", fake)
    monkeypatch.setattr(scraper, "fetch_market_row", lambda mid: SimpleNamespace(market_data=dict(DATA)))


def run(vault, mid=MID, p=0.8):
    phases.phase4_execution(vault, [{"market_id": mid, "p_value": p, "p_low": 0.7, "p_high": 0.9,
                                     "market_data": dict(DATA)}])


def stages(vault, mid=MID):
    return [r["stage"] for r in ledger.iter_rows(vault) if r["market_id"] == mid]


def offers(vault):
    return sorted(notify.outbox_dir(vault).glob("offer-*.json"))


def vote(vault, offer_id, choice):
    (notify.inbox_dir(vault) / f"vote-{offer_id}.json").write_text(json.dumps({"offer_id": offer_id, "vote": choice}))


def pending_offer(vault, monkeypatch, approval=True):
    if approval:
        monkeypatch.setenv("CONCIERGE_APPROVAL", "1")
    quotes(monkeypatch)
    run(vault)
    (path,) = offers(vault)
    return json.loads(path.read_text())


def test_flag0_executes_and_writes_informational_offer(vault, monkeypatch):
    quotes(monkeypatch)
    run(vault)
    assert vault.read_trade_log_dict(MID)["allocation_usd"] > 0
    assert stages(vault) == ["traded"]
    (path,) = offers(vault)
    offer = json.loads(path.read_text())
    assert offer["mode"] == "informational" and offer["question"] == "Will X happen?"
    for key in ("side", "p", "p_low", "p_high", "q_entry", "edge", "allocation_usd", "shares", "ask",
                "depth_usd", "manipulation_risk", "integ_flags", "offer_id", "created_at", "expires_at"):
        assert key in offer
    assert path.name == f"offer-{offer['offer_id']}.json"


def test_flag1_parks_offer_and_does_not_execute(vault, monkeypatch):
    offer = pending_offer(vault, monkeypatch)
    assert offer["mode"] == "approval" and offer["status"] == "pending"
    assert vault.read_trade_log_dict(MID) is None
    assert vault.has_execution_intent(MID)
    assert stages(vault) == ["offer_pending"]
    run(vault)  # not re-offered while pending
    assert len(offers(vault)) == 1 and stages(vault) == ["offer_pending"]
    assert p4_execution.process_inbox(vault)["ignored"] == 0  # no vote: nothing executes
    assert vault.read_trade_log_dict(MID) is None


def test_yes_vote_executes_when_edge_holds(vault, monkeypatch):
    offer = pending_offer(vault, monkeypatch)
    vote(vault, offer["offer_id"], "yes")
    assert p4_execution.process_inbox(vault)["approved"] == 1
    trade = vault.read_trade_log_dict(MID)
    assert trade["allocation_usd"] > 0 and not vault.has_execution_intent(MID)
    assert stages(vault) == ["offer_pending", "user_vote", "offer_approved", "traded"]
    assert not list(notify.inbox_dir(vault).glob("vote-*.json")) and not offers(vault)
    assert (notify.inbox_dir(vault) / "_done").exists() and (notify.outbox_dir(vault) / "_done").exists()


def test_yes_vote_after_edge_gone_is_stale(vault, monkeypatch):
    offer = pending_offer(vault, monkeypatch)
    quotes(monkeypatch, yes=0.79, no=0.25)  # ask moved up to our estimate
    vote(vault, offer["offer_id"], "yes")
    assert p4_execution.process_inbox(vault)["stale"] == 1
    assert "offer_stale" in stages(vault) and "traded" not in stages(vault)
    trade = vault.read_trade_log_dict(MID)
    assert trade["allocation_usd"] == 0 and trade["executed"] is False
    assert not vault.has_execution_intent(MID)


def test_retried_yes_vote_ledgers_user_vote_once(vault, monkeypatch):
    offer = pending_offer(vault, monkeypatch)
    good = polymarket_api.executable_quote
    monkeypatch.setattr(polymarket_api, "executable_quote", lambda m, s: None)  # quote fails
    vote(vault, offer["offer_id"], "yes")
    assert p4_execution.process_inbox(vault)["retry"] == 1
    assert p4_execution.process_inbox(vault)["retry"] == 1
    assert (notify.inbox_dir(vault) / f"vote-{offer['offer_id']}.json").exists()
    monkeypatch.setattr(polymarket_api, "executable_quote", good)
    assert p4_execution.process_inbox(vault)["approved"] == 1
    assert stages(vault).count("user_vote") == 1 and "traded" in stages(vault)


def test_no_vote_declines(vault, monkeypatch):
    offer = pending_offer(vault, monkeypatch)
    vote(vault, offer["offer_id"], "no")
    assert p4_execution.process_inbox(vault)["declined"] == 1
    assert stages(vault) == ["offer_pending", "user_vote", "offer_declined"]
    assert not vault.has_execution_intent(MID)
    run(vault)  # a declined market is not re-offered
    assert len(stages(vault)) == 3 and not offers(vault)


def test_expired_offer_ignores_yes_vote_and_sweeps_unvoted(vault, monkeypatch):
    offer = pending_offer(vault, monkeypatch)
    later = datetime.now(tz=timezone.utc) + timedelta(days=1)
    vote(vault, offer["offer_id"], "yes")
    assert p4_execution.process_inbox(vault, now=later)["expired"] == 1
    assert stages(vault) == ["offer_pending", "user_vote", "offer_expired"]
    assert vault.read_trade_log_dict(MID)["allocation_usd"] == 0

    # unvoted offer past expiry is swept too
    monkeypatch.setenv("CONCIERGE_APPROVAL", "1")
    phases.phase4_execution(vault, [{"market_id": "0xdef2", "p_value": 0.8, "market_data": dict(DATA)}])
    assert p4_execution.process_inbox(vault, now=later)["expired"] == 1
    assert stages(vault, "0xdef2") == ["offer_pending", "offer_expired"]


def test_unknown_or_bad_votes_are_ignored(vault):
    vote(vault, "nope-1", "yes")
    (notify.inbox_dir(vault) / "vote-bad.json").write_text("{not json")
    assert p4_execution.process_inbox(vault)["ignored"] == 2
    assert not list(notify.inbox_dir(vault).glob("vote-*.json"))


def test_phase5_skips_pending_and_vote_rows_but_keeps_final_offers(vault, monkeypatch):
    offer = pending_offer(vault, monkeypatch)
    assert ledger.unresolved_market_ids(vault) == []
    vote(vault, offer["offer_id"], "no")
    p4_execution.process_inbox(vault)
    assert ledger.unresolved_market_ids(vault) == [MID]  # declined: needs a resolution for counterfactual P&L


def test_offers_stats_section_and_counterfactual(vault, monkeypatch):
    offer = pending_offer(vault, monkeypatch)
    rows = list(ledger.iter_rows(vault))
    assert stats.offers_stats(rows)["pending"] == 1
    vote(vault, offer["offer_id"], "no")
    p4_execution.process_inbox(vault)
    ledger.record_resolution(vault, MID, 1)
    rows = list(ledger.iter_rows(vault))
    s = stats.compute_stats(rows)
    assert s["offers"]["pending"] == 0 and s["offers"]["counts"]["declined"] == 1
    grp = s["offers"]["groups"]["declined"]
    assert grp["n_resolved"] == 1 and grp["pnl"] > 0 and grp["brier_p"] is not None
    assert s["counterfactual_by_stage"]["offer_declined"][0] == 1
    text = stats.render_stats(s)
    assert "## Offers & votes" in text and "### Brier by runner backend" in text
