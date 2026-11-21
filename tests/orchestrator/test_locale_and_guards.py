"""Local-language search, thin-evidence guard, concentration caps, event-sibling guard, per-market bundles."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from config.trading_constants import (
    MAX_FULL_PER_COUNTRY_PER_TICK,
    RESEARCH_LOCAL_EXTRA_WEB_CALLS,
    RESEARCH_MAX_WEB_CALLS,
    RESEARCH_THIN_EVIDENCE_MAX_GAP,
)
from orchestrator import brave_search, evidence, ledger, phases
from orchestrator.directives import extract_filter_directives
from orchestrator.phases import p3_full, p3_queue, p3_research, p4_execution
from orchestrator.phases.common import Phase3Candidate
from orchestrator.phases.p2_routing import normalize_country
from orchestrator.runner_stubs import STUB_RESPONSES
from tests.orchestrator.test_research_redesign import World, _seed, _skips

TODAY = "2026-11-19"
CJK_Q = "".join(chr(c) for c in (0x65B0, 0x7AF9, 0x5E02, 0x9577, 0x9078, 0x8209, 0x6C11, 0x8ABF))
FULLWIDTH_QMARK = chr(0xFF1F)


# ---- 1. Brave locale params, CJK -------------------------------------------------------------------


class _Resp:
    status_code = 200
    headers: dict[str, str] = {}

    def json(self):
        return {"web": {"results": [{"url": "https://a.org"}]}}


def test_brave_search_sends_validated_country_and_lang(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    seen = []
    monkeypatch.setattr(brave_search.requests, "get", lambda url, **kw: seen.append(kw["params"]) or _Resp())
    brave_search.search("q", country="tw", search_lang="zh-tw")
    assert seen[-1]["country"] == "TW" and seen[-1]["search_lang"] == "zh-hant"  # alias mapped
    brave_search.search("q", country="ZZ", search_lang="klingon")
    assert "country" not in seen[-1] and "search_lang" not in seen[-1]  # unsupported: fall back to none
    brave_search.search("q")
    assert "country" not in seen[-1]
    assert brave_search.valid_country(None) is None and brave_search.valid_search_lang("ja") == "jp"


def test_keyword_query_keeps_cjk_text():
    assert brave_search.keyword_query(CJK_Q + FULLWIDTH_QMARK) == CJK_Q
    assert brave_search.keyword_query("Will " + CJK_Q + " win?") == "Will " + CJK_Q + " win"  # CJK: no English stripping
    assert brave_search.keyword_query("What is the poll? (as of 2026-11-19)") == "the poll"


def test_search_pages_forwards_locale(monkeypatch):
    got = {}
    monkeypatch.setattr(
        brave_search, "search",
        lambda q, freshness=None, country=None, search_lang=None: got.update(q=q, country=country, lang=search_lang) or [],
    )
    brave_search.search_pages(CJK_Q, country="TW", search_lang="zh-hant")
    assert got == {"q": CJK_Q, "country": "TW", "lang": "zh-hant"}


def test_brave_one_passes_locale_only_when_given(monkeypatch):
    calls = []
    monkeypatch.setattr(brave_search, "search_pages", lambda q, **kw: calls.append(kw) or [])
    evidence._brave_one(lambda r, p: None, "m", "q", TODAY)
    evidence._brave_one(lambda r, p: None, "m", "q", TODAY, {"country": "TW", "language": "zh-hant"})
    assert calls == [{}, {"country": "TW", "search_lang": "zh-hant"}]


# ---- locale plumbing in the crux pipeline ------------------------------------------------------------


def test_normalize_locale():
    assert p3_research.normalize_locale({"country": "tw", "language": "ZH-Hant"}) == {"country": "TW", "language": "zh-hant"}
    assert p3_research.normalize_locale({"country": "XX", "language": "fr"}) == {"country": None, "language": "fr"}
    assert p3_research.normalize_locale({"country": "US"}) is None and p3_research.normalize_locale(None) is None
    assert p3_research.is_non_english({"country": "TW", "language": "zh-hant"})
    assert not p3_research.is_non_english({"country": "US", "language": "en"}) and not p3_research.is_non_english(None)


def _row(mid="m1"):
    return {"market_id": mid, "market_title": "Title " + mid, "market_description": "d", "market_data": {}}


def _cruxes(with_local=True):
    roles = ["mechanism", "comparator", "evidence", "horizon", "skeptic", "evidence"]
    return [
        {"role": r, "question": f"q{i} {r}?", "queries": [f"en {i} as of {TODAY}"], "moves_p_if": "x",
         **({"queries_local": [f"{CJK_Q} {i}"]} if with_local else {})}
        for i, r in enumerate(roles)
    ]


def test_normalize_cruxes_keeps_one_local_query_for_allowed_roles_non_english_only():
    zh = {"country": "TW", "language": "zh-hant"}
    out = p3_research._normalize_cruxes(_cruxes(), _row(), TODAY, "full", None, zh)
    by_role = {c["role"]: c["queries_local"] for c in out}
    assert by_role["mechanism"] == [] and by_role["skeptic"] == []
    assert all(len(c["queries_local"]) == 1 for c in out if c["role"] in ("evidence", "comparator", "horizon"))
    en = p3_research._normalize_cruxes(_cruxes(), _row(), TODAY, "full", None, {"country": "US", "language": "en"})
    assert all(c["queries_local"] == [] for c in en)
    assert all(c["queries_local"] == [] for c in p3_research._normalize_cruxes(_cruxes(), _row(), TODAY, "full"))


class _Runner:
    def __init__(self, locale, local=True):
        self.locale, self.local = locale, local

    def __call__(self, role, payload):
        if role == "scout":
            return {**STUB_RESPONSES["scout"](payload), "locale": self.locale}
        if role == "briefer":
            return {"market_id": payload["market_id"], "cruxes": _cruxes(self.local), "error": None}
        return STUB_RESPONSES[role](payload)


@pytest.fixture()
def web_world(monkeypatch, vault):
    monkeypatch.setenv("PMBOT_EVIDENCE_SOURCE", "web")
    fetched: list[tuple[list[str], Any]] = []

    def fake(runner, vault_, market_id, queries, today, locale=None):
        fetched.append((list(queries), locale))
        return [
            {"query": q, "research_data": f"- fact {q} | https://x.org/{abs(hash(q)) % 999} | {today}", "error": None}
            for q in queries
        ]

    monkeypatch.setattr(evidence, "fetch_web_bundle", fake)
    return fetched


def test_non_english_locale_runs_local_queries_with_extra_budget(vault, web_world, caplog):
    import logging

    caplog.set_level(logging.INFO)
    zh = {"country": "TW", "language": "zh-hant"}
    _, meta, _, _ = p3_research.build_evidence(vault, _Runner(zh), _row(), TODAY, "full")
    local = [(q, loc) for qs, loc in web_world for q in qs if loc]
    assert len(local) == meta["local_queries"] == 4  # evidence x2, comparator, horizon
    assert all(loc == zh and CJK_Q in q for q, loc in local)
    assert meta["locale_country"] == "TW" and meta["locale_language"] == "zh-hant"
    assert meta["web_calls"] == 1 + 6 + 4 <= RESEARCH_MAX_WEB_CALLS + RESEARCH_LOCAL_EXTRA_WEB_CALLS  # scout + cruxes + local
    assert any("[LOCALE] m1: country=TW lang=zh-hant local_queries=4" in r.message for r in caplog.records)
    fields = p3_research.crux_fields(meta, None, [])
    assert (fields["locale_country"], fields["locale_language"], fields["local_queries"]) == ("TW", "zh-hant", 4)
    assert vault.read_research_bundle_meta("m1")["crux_meta"]["local_queries"] == 4


def test_english_locale_adds_no_local_queries_and_no_extra_budget(vault, web_world):
    _, meta, _, _ = p3_research.build_evidence(
        vault, _Runner({"country": "US", "language": "en"}), _row(), TODAY, "full"
    )
    assert meta["local_queries"] == 0 and not any(loc for _, loc in web_world)
    assert meta["web_calls"] <= RESEARCH_MAX_WEB_CALLS
    assert meta["locale_language"] == "en"


def test_missing_locale_is_english_only(vault, web_world):
    _, meta, _, _ = p3_research.build_evidence(vault, _Runner(None), _row(), TODAY, "full")
    assert meta["local_queries"] == 0 and meta["locale_language"] is None and meta["web_calls"] <= RESEARCH_MAX_WEB_CALLS


def test_local_queries_cannot_exceed_the_raised_cap(vault, web_world, monkeypatch):
    monkeypatch.setattr(p3_research, "RESEARCH_LOCAL_EXTRA_WEB_CALLS", 1)
    zh = {"country": "TW", "language": "zh-hant"}
    _, meta, _, _ = p3_research.build_evidence(vault, _Runner(zh), _row(), TODAY, "full")
    assert meta["web_calls"] <= RESEARCH_MAX_WEB_CALLS + 1
    assert meta["local_queries"] == 3  # the gap-round reserve (3 calls) stays free, so the fourth local query is dropped


def test_web_budget_extend():
    b = p3_research.WebBudget(2)
    b.extend(3)
    assert b.total == 5 and b.left == 5 and b.take(5) and b.used == 5


# ---- 2. thin-evidence guard ---------------------------------------------------------------------------


def _meta(*pairs):
    return {"cruxes": [{"role": r, "status": s} for r, s in pairs]}


def test_thin_evidence_unit():
    thin = p3_full.thin_evidence_disagreement
    m = _meta(("evidence", "unanswered"), ("mechanism", "answered"))
    assert thin(m, 0.45, 0.11, RESEARCH_THIN_EVIDENCE_MAX_GAP)
    assert not thin(m, 0.20, 0.11, RESEARCH_THIN_EVIDENCE_MAX_GAP)  # within the gap
    assert not thin(m, 0.45, None, RESEARCH_THIN_EVIDENCE_MAX_GAP)
    assert not thin(_meta(("evidence", "answered"), ("skeptic", "unanswered")), 0.45, 0.11, RESEARCH_THIN_EVIDENCE_MAX_GAP)  # evidence cruxes only
    assert not thin(None, 0.45, 0.11, RESEARCH_THIN_EVIDENCE_MAX_GAP)


def test_thin_evidence_knob_default_is_015():
    assert extract_filter_directives("")["research_thin_evidence_max_gap"] == RESEARCH_THIN_EVIDENCE_MAX_GAP
    md = "## Filter Weightings\n```yaml\nfilters:\n  research_thin_evidence_max_gap: 0.3\n```\n"
    assert extract_filter_directives(md)["research_thin_evidence_max_gap"] == 0.3


class _ThinWorld(World):
    unanswered: set[str] = set()

    def runner(self, role, payload):
        out = super().runner(role, payload)
        if role == "gap_analyzer":
            out = {**out, "assessments": [
                {**a, "status": "unanswered"} if a["crux_id"] in self.unanswered else a for a in out["assessments"]
            ]}
        return out


def test_thin_evidence_disagreement_skips_with_ledger_and_state(monkeypatch, vault):
    w = _ThinWorld(monkeypatch, vault)
    w.market("a", 0.42)
    _seed(vault, "a")
    w.dr_p = 0.6  # gap 0.18 > 0.15, below the misread gap
    w.unanswered = {"c3"}  # legacy architect slots: mechanism, comparator, evidence, skeptic (+ horizon template)
    out = phases.phase3_qualitative_pipeline(vault, runner=w.runner)
    assert out == []
    assert [s["reason"] for s in _skips(vault, "a")] == ["thin_evidence_disagreement"]
    assert vault.read_filter_log("a")["research_state"]["outcome"] == "thin_evidence_disagreement"
    assert "thin_evidence_disagreement" in p3_queue._SKIP_OUTCOMES


def test_answered_evidence_crux_still_researches(monkeypatch, vault):
    w = _ThinWorld(monkeypatch, vault)
    w.market("a", 0.42)
    _seed(vault, "a")
    w.unanswered = set()
    assert len(phases.phase3_qualitative_pipeline(vault, runner=w.runner)) == 1


# ---- 3. concentration caps ----------------------------------------------------------------------------


def _job(mid, event=None, country=None):
    triage = {"researchable": True, **({"country": country} if country else {})}
    cand = Phase3Candidate(market_id=mid, kind="initial", filter_record={"triage": triage})
    return p3_queue._Job(cand, {"market_id": mid, "market_data": {"event_id": event} if event else {}})


def test_event_cap_keeps_highest_ranked_and_defers_rest(caplog):
    import logging

    caplog.set_level(logging.INFO)
    kept, deferred = p3_queue._apply_concentration_caps(
        [_job("a", "e1"), _job("b", "e1"), _job("c", "e2"), _job("d")]
    )
    assert [j.market_id for j in kept] == ["a", "c", "d"] and [j.market_id for j in deferred] == ["b"]
    assert any("skip:event_cap" in r.message for r in caplog.records)


def test_country_cap_and_missing_or_global_country_uncapped():
    jobs = (
        [_job(f"t{i}", country="TW") for i in range(4)] + [_job(f"g{i}", country="global") for i in range(3)]
        + [_job(f"n{i}") for i in range(3)] + [_job("u1", country="US")]
    )
    kept, deferred = p3_queue._apply_concentration_caps(jobs)
    assert [j.market_id for j in kept if j.market_id.startswith("t")] == [f"t{i}" for i in range(MAX_FULL_PER_COUNTRY_PER_TICK)]
    assert [j.market_id for j in deferred] == ["t2", "t3"]
    assert len(kept) == 2 + 3 + 3 + 1


def test_taiwan_markets_in_distinct_events_are_country_capped(monkeypatch, vault):
    w = World(monkeypatch, vault)
    for i in range(4):
        w.market(f"t{i}", 0.42, event_id=f"ev{i}")
        _seed(vault, f"t{i}", score=1.0 - i / 10, triage={"researchable": True, "country": "TW"})
    out = phases.phase3_qualitative_pipeline(vault, runner=w.runner)
    assert sorted(r["market_id"] for r in out) == ["t0", "t1"]
    assert not _skips(vault) and not vault.is_market_inactive("t2")  # deferred, not failed


def test_normalize_country():
    assert normalize_country("tw") == "TW" and normalize_country("Global") == "global"
    assert normalize_country(None) is None and normalize_country("Taiwan") is None and normalize_country("1a") is None
    assert STUB_RESPONSES["triage"]({})["country"] == "global"


# ---- 4. event-sibling guard ---------------------------------------------------------------------------


def _sibling(vault, mid, event, outcome, p=None, days_ago=0.5):
    at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    _seed(vault, mid, event_id=event, research_state={"stage": "full", "outcome": outcome, "at": at, "p": p})


def test_sibling_guard_bad_sibling_outcomes(vault):
    md = {"event_id": "e1"}
    assert p4_execution.event_sibling_guard(vault, "me", md, 0.5) is None
    _sibling(vault, "s1", "e1", "probable_misread")
    assert "probable_misread" in p4_execution.event_sibling_guard(vault, "me", md, 0.5)
    assert p4_execution.event_sibling_guard(vault, "me", {"event_id": "other"}, 0.5) is None
    assert p4_execution.event_sibling_guard(vault, "s1", md, 0.5) is None  # a market is never its own sibling
    _sibling(vault, "s1", "e1", "thin_evidence_disagreement")
    assert "thin_evidence" in p4_execution.event_sibling_guard(vault, "me", md, 0.5)
    _sibling(vault, "s1", "e1", "probable_misread", days_ago=4)  # outside the 3 day window
    assert p4_execution.event_sibling_guard(vault, "me", md, 0.5) is None


def test_sibling_guard_probability_sum_only_for_neg_risk(vault):
    _sibling(vault, "s1", "e1", "researched", p=0.5)
    _sibling(vault, "s2", "e1", "researched", p=0.4)
    assert "sums to 1.35" in p4_execution.event_sibling_guard(vault, "me", {"event_id": "e1", "neg_risk": True}, 0.45)
    assert p4_execution.event_sibling_guard(vault, "me", {"event_id": "e1"}, 0.45) is None  # not mutually exclusive
    assert p4_execution.event_sibling_guard(vault, "me", {"event_id": "e1", "neg_risk": True}, 0.2) is None  # 1.1


def test_sibling_guard_blocks_trade_and_ledgers_once(monkeypatch, vault):
    w = World(monkeypatch, vault)
    w.market("me", 0.2, event_id="e1")
    _seed(vault, "me", event_id="e1")
    _sibling(vault, "s1", "e1", "probable_misread")
    row = {"market_id": "me", "p_value": 0.5, "p_low": 0.4, "p_high": 0.6, "market_data": w.rows["me"].market_data}
    for _ in range(2):
        p4_execution._execute_market(vault, row, True)
    assert vault.market_file("me", "trades") is None
    assert [s["reason"] for s in _skips(vault, "me")] == ["event_sibling_guard"]  # re-fires each tick, ledgered once
    assert not any(r["stage"] == "traded" for r in ledger.iter_rows(vault))


# ---- 5. no sibling bundle reuse -----------------------------------------------------------------------


def test_prepare_evidence_never_copies_a_sibling_bundle():
    import inspect

    assert "shared_bundle" not in inspect.signature(p3_full._prepare_evidence).parameters
    assert "shared_bundle" not in inspect.signature(p3_full._research_market).parameters
