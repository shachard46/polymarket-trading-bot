"""Phase 2 end to end: gate -> rank -> triage, budgets, caching, ledger."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from obsidian_utils import ObsidianManager
from orchestrator import ledger, phases
from orchestrator.scraper import MarketRow


@pytest.fixture()
def vault(tmp_path):
    v = ObsidianManager(vault_base=tmp_path)
    v.cold_start_protocol()
    return v


def mk(mid: str, *, vol: float = 1_000.0, event_id: str | None = None, **over: Any) -> MarketRow:
    md = {
        "yes_price": 0.4,
        "no_price": 0.6,
        "volume": vol,
        "liquidity": 20_000.0,
        "days_to_resolution": 30,
        "category": "politics",
    }
    if event_id:
        md["event_id"] = event_id
    md.update(over)
    return MarketRow(market_id=mid, market_title=f"Will {mid}?", market_data=md)


class Triage:
    def __init__(self, verdicts: dict[str, bool] | None = None):
        self.calls: list[dict[str, Any]] = []
        self.verdicts = verdicts or {}

    def __call__(self, role: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert role == "triage"
        self.calls.append(payload)
        return {
            "market_id": payload["market_id"],
            "researchable": self.verdicts.get(payload["market_id"], True),
            "category": "politics",
            "reason": "r",
            "error": None,
        }


def stages(vault, mid):
    return [r["stage"] for r in ledger.iter_rows(vault) if r["market_id"] == mid]


def test_triage_payload_has_no_price(vault):
    t = Triage()
    phases.phase2_quantitative_routing(vault, [mk("a")], runner=t)
    assert set(t.calls[0]) == {
        "market_id", "title", "description", "resolution_source", "category", "end_date", "today",
    }


def test_budgets_respected_and_ranked(vault, monkeypatch):
    monkeypatch.setenv("OPENCLAW_GATE_BUDGET", "4")
    monkeypatch.setenv("OPENCLAW_TRIAGE_BUDGET", "2")
    # lower volume => lower attention => higher score
    markets = [mk(f"m{i}", vol=1_000.0 * (i + 1) ** 2) for i in range(6)]
    t = Triage()
    phases.phase2_quantitative_routing(vault, markets, runner=t)
    assert sorted(c["market_id"] for c in t.calls) == ["m0", "m1", "m2", "m3"]
    passed = [m for m in ("m0", "m1", "m2", "m3", "m4", "m5") if vault.read_filter_log(m)["passed"]]
    assert passed == ["m0", "m1"]
    assert vault.read_filter_log("m3")["trigger"] == "over_triage_budget"
    assert vault.read_filter_log("m5")["trigger"] == "over_gate_budget"
    queue = [c.market_id for c in phases.build_phase3_queue(vault)]
    assert queue == ["m0", "m1"]
    rec = vault.read_filter_log("m0")
    assert rec["score"] == rec["confidence_multiplier"] > 0


def test_triage_once_per_event_and_cached_across_ticks(vault):
    t = Triage()
    markets = [mk("a", event_id="E1"), mk("b", event_id="E1"), mk("c", event_id="E2")]
    phases.phase2_quantitative_routing(vault, markets, runner=t)
    assert len(t.calls) == 2  # one for E1, one for E2
    assert vault.read_filter_log("b")["triage"]["researchable"] is True

    # next tick, markets due again (state is unchanged): no new triage calls
    t2 = Triage()
    phases.phase2_quantitative_routing(vault, markets, runner=t2)
    assert t2.calls == []
    # a new sibling of a known event reuses the verdict
    phases.phase2_quantitative_routing(vault, [mk("d", event_id="E1")], runner=t2)
    assert t2.calls == []
    assert vault.read_filter_log("d")["triage"]["researchable"] is True


def test_triage_reject_retires_and_ledgers(vault):
    t = Triage({"bad": False})
    phases.phase2_quantitative_routing(vault, [mk("bad"), mk("good")], runner=t)
    rec = vault.read_filter_log("bad")
    assert rec["status"] == "retired" and rec["passed"] is False
    assert rec["triage"]["researchable"] is False
    assert stages(vault, "bad") == ["triage_reject"]
    assert vault.read_filter_log("good")["passed"] is True

    # retired markets are skipped silently on later ticks
    t2 = Triage()
    phases.phase2_quantitative_routing(vault, [mk("bad")], runner=t2)
    assert t2.calls == [] and stages(vault, "bad") == ["triage_reject"]
    assert "bad" not in [c.market_id for c in phases.build_phase3_queue(vault)]


def test_event_reject_applies_to_siblings(vault):
    t = Triage({"a": False})
    phases.phase2_quantitative_routing(
        vault, [mk("a", event_id="E"), mk("b", event_id="E")], runner=t
    )
    assert len(t.calls) == 1
    assert vault.read_filter_log("b")["status"] == "retired"
    assert stages(vault, "b") == ["triage_reject"]


def test_gate_reject_ledgered_once_with_backoff(vault):
    far = mk("far", days_to_resolution=200)
    t = Triage()
    phases.phase2_quantitative_routing(vault, [far], runner=t)
    rec = vault.read_filter_log("far")
    assert rec["trigger"] == "too_far" and rec["fail_count"] == 1
    assert rec["next_check_at"] and rec["last_q"] == 0.4
    assert stages(vault, "far") == ["gate_reject"]
    assert t.calls == []

    # not due: nothing changes
    phases.phase2_quantitative_routing(vault, [far], runner=t)
    assert vault.read_filter_log("far")["fail_count"] == 1

    # make it due: fail again, still only one ledger row (no state change)
    vault.patch_frontmatter(
        "far", "filters",
        {"next_check_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()},
    )
    phases.phase2_quantitative_routing(vault, [far], runner=t)
    assert vault.read_filter_log("far")["fail_count"] == 2
    assert stages(vault, "far") == ["gate_reject"]


def test_retired_after_six_fails(vault):
    far = mk("far", days_to_resolution=200)
    for _ in range(6):
        phases.phase2_quantitative_routing(vault, [far], runner=Triage())
        vault.patch_frontmatter(
            "far", "filters",
            {"next_check_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()},
        )
    rec = vault.read_filter_log("far")
    assert rec["status"] == "retired" and rec["fail_count"] == 6


def test_arbitrage_ledgered_not_researched(vault):
    t = Triage()
    phases.phase2_quantitative_routing(
        vault, [mk("arb", yes_price=0.45, no_price=0.5)], runner=t
    )
    assert t.calls == []
    assert stages(vault, "arb") == ["arb_opportunity"]
    phases.phase2_quantitative_routing(
        vault, [mk("arb", yes_price=0.45, no_price=0.5)], runner=t
    )
    assert stages(vault, "arb") == ["arb_opportunity"]  # deduped
    assert vault.read_filter_log("arb") is None


def test_triage_agent_error_flags_inactive(vault):
    def bad(role, payload):
        return {"market_id": payload["market_id"], "researchable": True,
                "category": "x", "reason": "r", "error": "boom"}

    phases.phase2_quantitative_routing(vault, [mk("a")], runner=bad)
    assert vault.is_market_inactive("a")


