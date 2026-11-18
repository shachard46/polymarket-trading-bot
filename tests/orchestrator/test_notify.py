"""Outbox digest, Discord webhook, loop wiring."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import requests

from obsidian_utils import ObsidianManager
from orchestrator import ledger, loop, notify, stats


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    monkeypatch.delenv("CONCIERGE_APPROVAL", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    v = ObsidianManager(vault_base=tmp_path)
    v.cold_start_protocol()
    return v


def test_digest_written_once_per_utc_day(vault):
    now = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)
    ledger.record(vault, "0xa", "traded", q=0.3, p=0.6, price=0.32, size_usd=10, side="YES", shares=31.0)
    path = notify.emit_digest_if_due(vault, now)
    assert path is not None and path.name == "digest-2026-05-01.json"
    digest = notify.read_json(path)
    for key in ("realised_pnl_after_tax", "counterfactual_pnl_after_tax", "brier_p", "brier_q",
                "resolved_this_week", "new_trades", "new_offers", "integrity_rejects",
                "pending_directive_proposal"):
        assert key in digest
    assert notify.emit_digest_if_due(vault, now + timedelta(hours=3)) is None
    assert notify.emit_digest_if_due(vault, now + timedelta(days=1)) is not None


def test_digest_failure_never_raises(vault, monkeypatch):
    monkeypatch.setattr(notify, "build_digest", lambda *a, **k: 1 / 0)
    assert notify.emit_digest_if_due(vault) is None


def test_webhook_failure_does_not_raise_and_success_posts(vault, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
    calls = []

    def boom(url, json=None, timeout=None):
        calls.append((url, timeout))
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "post", boom)
    assert notify.post_discord("hi") is False and calls == [("https://example.invalid/hook", 5)]
    offer = notify.emit_offer(vault, {"market_id": "0xb", "side": "YES"}, {}, mode="informational")
    assert offer is not None  # file still written
    assert notify.digest_path(vault, "x").parent.exists()


def test_no_webhook_means_no_post(vault, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("should not post"))
    assert notify.post_discord("hi") is False


def test_tick_runs_digest_and_sets_nonce(vault, monkeypatch):
    seen = []
    monkeypatch.setattr(loop, "phase1_data_ingestion", lambda v: [])
    monkeypatch.setattr(loop, "phase2_quantitative_routing", lambda v, t: None)
    monkeypatch.setattr(loop, "phase3_qualitative_pipeline", lambda v: [])
    monkeypatch.setattr(loop, "phase4_execution", lambda v, r: None)
    monkeypatch.setattr(loop, "phase5_resolution_and_post_mortem", lambda v: None)
    monkeypatch.setattr(loop._runner, "set_run_nonce", seen.append, raising=False)
    loop.run_pipeline_tick(vault)
    assert len(seen) == 1 and list(notify.outbox_dir(vault).glob("digest-*.json"))


def test_sleep_polls_inbox_only_with_approval(vault, monkeypatch):
    monkeypatch.setenv("CONCIERGE_POLL_SEC", "10")
    sleeps, polls = [], []
    monkeypatch.setattr(loop.time, "sleep", sleeps.append)
    monkeypatch.setattr(loop, "process_inbox", lambda v: polls.append(1))
    loop._sleep_polling_inbox(vault, 25)
    assert sleeps == [10, 10, 5] and polls == []
    monkeypatch.setenv("CONCIERGE_APPROVAL", "1")
    loop._sleep_polling_inbox(vault, 25)
    assert len(polls) == 3


def test_runner_backend_stats_tolerates_absence():
    rows = [
        {"stage": "traded", "market_id": "a", "p": 0.7, "q": 0.5},
        {"stage": "resolution", "market_id": "a", "outcome_yes": 1},
    ]
    assert list(stats.brier_by_runner_backend(rows)) == ["unknown"]
    rows[0]["runner_backend"] = "direct"
    assert list(stats.brier_by_runner_backend(rows)) == ["direct"]


def test_write_offer_stringifies_datetime(vault):
    offer = {"offer_id": "o1", "created_at": datetime(2026, 5, 1, 12, tzinfo=timezone.utc)}
    path = notify.write_offer(vault, offer)
    assert notify.read_json(path)["created_at"] == "2026-05-01 12:00:00+00:00"
