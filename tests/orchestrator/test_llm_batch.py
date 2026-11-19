"""Batch API: complete_many template, Anthropic + Gemini adapters, spawn_many, p2 triage batching."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from obsidian_utils import ObsidianManager
from orchestrator import ledger, phases, runner
from orchestrator.llm import anthropic_llm, base, http_llm, usage
from orchestrator.llm.anthropic_llm import AnthropicLLM
from orchestrator.llm.base import LLM, LLMError, LLMRequest, LLMResponse
from orchestrator.llm.gemini_llm import GeminiLLM
from orchestrator.scraper import MarketRow


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setenv("PMBOT_LLM_BATCH_POLL_SEC", "0")
    monkeypatch.setenv("PMBOT_LLM_BATCH_MAX_WAIT_SEC", "5")
    monkeypatch.setenv("PMBOT_LLM_RETRY_BACKOFF", "0")
    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    rows: list[tuple] = []
    monkeypatch.setattr(usage, "record", lambda role, resp, batch=False: rows.append((role, resp.text, batch)))
    return rows


@pytest.fixture
def usage_rows(_fast):
    return _fast


def reqs(n=3):
    return [LLMRequest("sys", f"user{i}", market_id=f"m{i}") for i in range(n)]


# ---- base template -----------------------------------------------------------------------------


class FakeSync(LLM):
    provider_name = "fake"
    api_key_env = "FAKE_KEY"

    def __init__(self, **kw):
        super().__init__("fake-model", **kw)
        self.sent: list[str] = []

    def _send(self, system, user, json_mode, web):
        self.sent.append(user)
        if user == "user1" and getattr(self, "fail1", False):
            raise LLMError("boom")
        return LLMResponse(f"sync:{user}", "fake", "fake-model", 1, 2)

    def _is_retryable(self, exc):
        return False


class FakeBatch(FakeSync):
    supports_batch = True

    def __init__(self, states=("done",), results=None, **kw):
        super().__init__(**kw)
        self.states = list(states)
        self.results = results
        self.cancelled = False
        self.submitted: list[LLMRequest] = []

    def _batch_submit(self, requests):
        self.submitted = list(requests)
        return "h"

    def _batch_poll(self, handle):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def _batch_fetch(self, handle, count):
        if self.results is not None:
            return self.results
        return [LLMResponse(f"batch:{r.user}", "fake", "fake-model", 3, 4) for r in self.submitted]

    def _batch_cancel(self, handle):
        self.cancelled = True


def test_sequential_when_unsupported_or_single(usage_rows):
    llm = FakeSync()
    out = llm.complete_many(reqs(3))
    assert [r.text for r in out] == ["sync:user0", "sync:user1", "sync:user2"]
    b = FakeBatch()
    assert [r.text for r in b.complete_many(reqs(1))] == ["sync:user0"]
    assert not b.submitted
    b2 = FakeBatch()
    b2.batch_enabled = False
    assert [r.text for r in b2.complete_many(reqs(2))] == ["sync:user0", "sync:user1"]


def test_batch_happy_path_polls_and_records_usage(usage_rows):
    llm = FakeBatch(states=("pending", "pending", "done"))
    llm.role = "triage"
    out = llm.complete_many(reqs(3))
    assert [r.text for r in out] == ["batch:user0", "batch:user1", "batch:user2"]
    assert llm.sent == [] and not llm.cancelled
    assert usage_rows == [("triage", f"batch:user{i}", True) for i in range(3)]


def test_web_items_stay_sync(usage_rows):
    llm = FakeBatch()
    items = [LLMRequest("s", "user0"), LLMRequest("s", "user1", web=True), LLMRequest("s", "user2")]
    out = llm.complete_many(items)
    assert [r.text for r in out] == ["batch:user0", "sync:user1", "batch:user2"]


def test_timeout_cancels_and_falls_back_to_sync(monkeypatch, usage_rows):
    monkeypatch.setenv("PMBOT_LLM_BATCH_MAX_WAIT_SEC", "0")
    llm = FakeBatch(states=("pending",))
    out = llm.complete_many(reqs(2))
    assert llm.cancelled
    assert [r.text for r in out] == ["sync:user0", "sync:user1"]
    assert all(row[2] is False for row in usage_rows)


def test_failed_batch_and_submit_error_fall_back(usage_rows):
    llm = FakeBatch(states=("failed",))
    assert [r.text for r in llm.complete_many(reqs(2))] == ["sync:user0", "sync:user1"]
    assert llm.cancelled

    class BadSubmit(FakeBatch):
        def _batch_submit(self, requests):
            raise RuntimeError("down")

    assert [r.text for r in BadSubmit().complete_many(reqs(2))] == ["sync:user0", "sync:user1"]


def test_per_item_errors_and_missing_results(usage_rows):
    err = LLMError("item bad")
    llm = FakeBatch(results=[LLMResponse("ok", "fake", "fake-model"), err, None])
    out = llm.complete_many(reqs(3))
    assert out[0].text == "ok"
    assert out[1] is err
    assert out[2].text == "sync:user2"  # missing -> sync fallback


def test_sync_item_error_is_returned_not_raised(usage_rows):
    llm = FakeSync()
    llm.fail1 = True
    out = llm.complete_many(reqs(3))
    assert isinstance(out[1], LLMError) and out[0].text == "sync:user0" and out[2].text == "sync:user2"


# ---- Anthropic ---------------------------------------------------------------------------------


class FakeBatches:
    def __init__(self, statuses, entries):
        self.statuses = list(statuses)
        self.entries = entries
        self.created = None
        self.cancelled = None

    def create(self, requests):
        self.created = requests
        return SimpleNamespace(id="msgbatch_1")

    def retrieve(self, bid):
        st = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return SimpleNamespace(processing_status=st)

    def results(self, bid):
        return iter(self.entries)

    def cancel(self, bid):
        self.cancelled = bid


def _msg(text):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)], usage=SimpleNamespace(input_tokens=5, output_tokens=6),
        stop_reason="end_turn",
    )


def _entry(i, kind, text=None, err=None):
    res = SimpleNamespace(type=kind)
    if kind == "succeeded":
        res.message = _msg(text)
    if kind == "errored":
        res.error = SimpleNamespace(error=SimpleNamespace(message=err))
    return SimpleNamespace(custom_id=f"r{i}", result=res)


def _fake_anthropic(monkeypatch, batches):
    client = SimpleNamespace(messages=SimpleNamespace(batches=batches, create=lambda **kw: _msg("sync")))
    mod = SimpleNamespace(Anthropic=lambda **kw: client)
    monkeypatch.setattr(anthropic_llm, "_import_anthropic", lambda: mod)


def test_anthropic_batch_out_of_order_and_errors(monkeypatch, usage_rows):
    entries = [
        _entry(2, "succeeded", "c"),
        _entry(0, "succeeded", "a"),
        _entry(1, "errored", err="overloaded"),
    ]
    batches = FakeBatches(["in_progress", "ended"], entries)
    _fake_anthropic(monkeypatch, batches)
    llm = AnthropicLLM(model="claude-x")
    llm.role = "triage"
    out = llm.complete_many(reqs(3))
    assert out[0].text == "a" and out[2].text == "c"
    assert isinstance(out[1], LLMError) and "overloaded" in str(out[1])
    assert out[0].input_tokens == 5 and out[0].output_tokens == 6
    assert [r["custom_id"] for r in batches.created] == ["r0", "r1", "r2"]
    assert batches.created[0]["params"]["model"] == "claude-x"
    assert [row[2] for row in usage_rows] == [True, True]


def test_anthropic_timeout_cancels_then_sync(monkeypatch, usage_rows):
    monkeypatch.setenv("PMBOT_LLM_BATCH_MAX_WAIT_SEC", "0")
    batches = FakeBatches(["in_progress"], [])
    _fake_anthropic(monkeypatch, batches)
    out = AnthropicLLM(model="claude-x").complete_many(reqs(2))
    assert batches.cancelled == "msgbatch_1"
    assert [r.text for r in out] == ["sync", "sync"]


def test_anthropic_expired_item_falls_back(monkeypatch, usage_rows):
    batches = FakeBatches(["ended"], [_entry(0, "succeeded", "a"), _entry(1, "expired")])
    _fake_anthropic(monkeypatch, batches)
    out = AnthropicLLM(model="claude-x").complete_many(reqs(2))
    assert [r.text for r in out] == ["a", "sync"]


# ---- Gemini ------------------------------------------------------------------------------------


def _gresp(text):
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3},
    }


class GemHttp:
    def __init__(self, poll_data):
        self.polls = list(poll_data)
        self.posts: list[tuple[str, Any]] = []
        self.gets: list[str] = []

    def install(self, monkeypatch):
        def post(self_, url, headers, body):
            self.posts.append((url, body))
            if url.endswith(":batchGenerateContent"):
                return {"name": "batches/abc"}
            return {}

        def get(self_, url, headers):
            self.gets.append(url)
            return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]

        monkeypatch.setattr(http_llm.HttpLLM, "_post", post)
        monkeypatch.setattr(http_llm.HttpLLM, "_get", get)


@pytest.fixture
def gem(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    g = GeminiLLM(model="gemini-x")
    g.role = "triage"
    return g


def test_gemini_variant_dest_inlined(monkeypatch, gem, usage_rows):
    http = GemHttp([
        {"name": "batches/abc", "state": "JOB_STATE_RUNNING"},
        {"name": "batches/abc", "state": "JOB_STATE_SUCCEEDED", "dest": {"inlinedResponses": [
            {"response": _gresp("a")}, {"error": {"message": "bad item"}}, {"response": _gresp("c")},
        ]}},
    ])
    http.install(monkeypatch)
    out = gem.complete_many(reqs(3))
    assert out[0].text == "a" and out[2].text == "c" and isinstance(out[1], LLMError)
    url, body = http.posts[0]
    assert url.endswith("/models/gemini-x:batchGenerateContent")
    inner = body["batch"]["input_config"]["requests"]["requests"]
    assert [r["metadata"]["key"] for r in inner] == ["k0", "k1", "k2"]
    assert inner[0]["request"]["contents"][0]["parts"][0]["text"] == "user0"
    assert http.gets[0].endswith("/batches/abc")


def test_gemini_variant_metadata_state_response_inlined(monkeypatch, gem, usage_rows):
    http = GemHttp([{
        "name": "batches/abc", "metadata": {"state": "JOB_STATE_SUCCEEDED"},
        "response": {"inlinedResponses": {"inlinedResponses": [
            {"response": _gresp("x"), "metadata": {"key": "k0"}},
            {"response": _gresp("y"), "metadata": {"key": "k1"}},
        ]}},
    }])
    http.install(monkeypatch)
    assert [r.text for r in gem.complete_many(reqs(2))] == ["x", "y"]


def test_gemini_failed_state_cancels_and_goes_sync(monkeypatch, gem, usage_rows):
    http = GemHttp([{"name": "batches/abc", "state": "JOB_STATE_FAILED"}])
    http.install(monkeypatch)
    monkeypatch.setattr(http_llm.HttpLLM, "_send", lambda self, s, u, j, w: LLMResponse("sync:" + u, "gemini", "gemini-x"))
    out = gem.complete_many(reqs(2))
    assert [r.text for r in out] == ["sync:user0", "sync:user1"]
    assert http.posts[-1][0].endswith("/batches/abc:cancel")


# ---- spawn_many --------------------------------------------------------------------------------


def _triage_payload(mid):
    return {"market_id": mid, "title": "t", "description": "d", "resolution_source": None,
            "category": "politics", "end_date": None, "today": "2026-01-01"}


def _triage_json(mid, researchable=True):
    return json.dumps({"market_id": mid, "researchable": researchable, "category": "politics", "reason": "r",
                       "manipulation_risk": "low", "manipulation_reason": "mr", "error": None})


def test_spawn_many_stub_matches_spawn_agent(monkeypatch):
    monkeypatch.setenv("PMBOT_MODE", "stub")
    payloads = [_triage_payload(f"m{i}") for i in range(3)]
    assert runner.spawn_many("triage", payloads) == [runner.spawn_agent("triage", p) for p in payloads]


def test_spawn_many_live_order_and_single_item_fallback(monkeypatch):
    monkeypatch.setenv("PMBOT_MODE", "live")
    from orchestrator import direct_runner

    seen = {}

    def fake_run_many(role, spec, payloads):
        seen["n"] = len(payloads)
        return [_triage_json("m0"), "not json at all {", RuntimeError("batch item failed"), _triage_json("m3")]

    monkeypatch.setattr(direct_runner, "run_many", fake_run_many)
    monkeypatch.setattr(direct_runner, "run_direct", lambda role, spec, payload: _triage_json(payload["market_id"], False))
    out = runner.spawn_many("triage", [_triage_payload(f"m{i}") for i in range(4)])
    assert seen["n"] == 4
    assert json.loads(out[0])["market_id"] == "m0" and json.loads(out[0])["researchable"] is True
    # unparsable item: parsing happens in the phase, validation lets it through
    assert out[1] == "not json at all {"
    # errored item: retried singly via spawn_agent -> run_direct
    assert json.loads(out[2])["market_id"] == "m2" and json.loads(out[2])["researchable"] is False
    assert json.loads(out[3])["market_id"] == "m3"


def test_spawn_many_input_error_in_slot(monkeypatch):
    monkeypatch.setenv("PMBOT_MODE", "stub")
    out = runner.spawn_many("triage", [_triage_payload("m0"), {"bogus": 1}])
    assert isinstance(out[0], str) or isinstance(out[0], dict)
    assert isinstance(out[1], Exception)


def test_prefetch_runner_serves_first_call_then_real_runner(monkeypatch):
    monkeypatch.setenv("PMBOT_MODE", "stub")
    payloads = [_triage_payload("a"), _triage_payload("b")]
    calls = []
    monkeypatch.setattr(runner, "spawn_many", lambda role, ps: calls.append(len(ps)) or ["RA", ValueError("rb")])
    pr = runner.prefetch_runner(runner.spawn_agent, "triage", payloads)
    assert pr("triage", payloads[0]) == "RA"
    with pytest.raises(ValueError):
        pr("triage", payloads[1])
    assert pr("triage", payloads[0]) == runner.spawn_agent("triage", payloads[0])  # consumed -> real runner
    assert calls == [2]
    custom = lambda r, p: "x"  # noqa: E731
    assert runner.prefetch_runner(custom, "triage", payloads) is custom


# ---- p2: batched == unbatched --------------------------------------------------------------------


def _mk(mid):
    md = {"yes_price": 0.4, "no_price": 0.6, "volume": 1000.0 * (1 + int(mid[1:])), "liquidity": 20000.0,
          "days_to_resolution": 30, "category": "politics"}
    return MarketRow(market_id=mid, market_title=f"Will {mid}?", market_data=md)


class FakeTriageLLM(LLM):
    provider_name = "fake"
    api_key_env = "FAKE_KEY"
    supports_batch = True

    def __init__(self):
        super().__init__("fake-model")
        self.many_calls: list[int] = []
        self.sync_calls = 0
        self.batch_calls = 0

    def _text(self, user):
        mid = user.split('"market_id": "')[1].split('"')[0]
        return _triage_json(mid, researchable=mid != "m1")

    def _send(self, system, user, json_mode, web):
        self.sync_calls += 1
        return LLMResponse(self._text(user), "fake", "fake-model", 1, 1)

    def _is_retryable(self, exc):
        return False

    def _batch_submit(self, requests):
        self.batch_calls += 1
        self.many_calls.append(len(requests))
        return list(requests)

    def _batch_poll(self, handle):
        return "done"

    def _batch_fetch(self, handle, count):
        return [LLMResponse(self._text(r.user), "fake", "fake-model", 1, 1) for r in handle]

    def _batch_cancel(self, handle):
        pass


def _run_p2(tmp_path, monkeypatch, batch: bool):
    from orchestrator.llm import routing

    llm = FakeTriageLLM()
    llm.batch_enabled = batch
    monkeypatch.setenv("PMBOT_MODE", "live")
    monkeypatch.setattr(routing, "llm_for", lambda role: llm)
    v = ObsidianManager(vault_base=tmp_path)
    v.cold_start_protocol()
    phases.phase2_quantitative_routing(v, [_mk(f"m{i}") for i in range(4)])
    recs = {}
    for i in range(4):
        r = dict(v.read_filter_log(f"m{i}"))
        for k in ("triage", "next_check_at"):
            if isinstance(r.get(k), dict):
                r[k] = {kk: vv for kk, vv in r[k].items() if kk != "triaged_at"}
        recs[f"m{i}"] = r
    stages = sorted((x["market_id"], x["stage"]) for x in ledger.iter_rows(v))
    return llm, recs, stages


def test_p2_triage_batched_equals_unbatched(tmp_path, monkeypatch):
    a, recs_a, stages_a = _run_p2(tmp_path / "a", monkeypatch, batch=True)
    b, recs_b, stages_b = _run_p2(tmp_path / "b", monkeypatch, batch=False)
    assert a.many_calls == [4] and a.sync_calls == 0
    assert b.batch_calls == 0 and b.sync_calls == 4
    assert stages_a == stages_b
    assert any(s == "triage_reject" for _m, s in stages_a)

    def norm(recs):
        return {m: {k: v for k, v in r.items() if k not in ("updated_at", "last_checked_at", "next_check_at")}
                for m, r in recs.items()}

    assert norm(recs_a) == norm(recs_b)


# ---- p3: triage shadow batched == unbatched ---------------------------------------------------


def _shadow_setup(monkeypatch, vault):
    from tests.orchestrator.test_research_redesign import World
    from tests.orchestrator.test_triage_shadow import _ids, _retired

    world = World(monkeypatch, vault)
    ids = _ids(True, 2)
    for mid in ids:
        world.market(mid, 0.5)
        _retired(vault, mid)
    return ids


def _shadow_rows(vault):
    skip = {"ts", "ts_utc", "timestamp", "id"}
    return sorted(
        ({k: v for k, v in r.items() if k not in skip} for r in ledger.iter_rows(vault) if r["stage"] == "triage_shadow"),
        key=lambda r: r["market_id"],
    )


def _run_shadows(vault, plain=False):
    from orchestrator.live_prompt import today_date
    from orchestrator.phases import p3_queue

    rn = (lambda r, p: runner.spawn_agent(r, p)) if plain else runner.spawn_agent  # plain: no prefetch/batching
    return p3_queue._run_triage_shadows(vault, rn, "", str(today_date()), lambda: False)


def test_p3_triage_shadow_batched_equals_unbatched_and_item_failure_falls_back(tmp_path, monkeypatch):
    from orchestrator import direct_runner, runner_stubs

    monkeypatch.setenv("PMBOT_MODE", "live")
    direct = []
    monkeypatch.setattr(
        direct_runner, "run_direct",
        lambda role, spec, payload: direct.append(payload["market_id"]) or runner_stubs._deep_researcher_stub(payload),
    )
    batches = []

    def run_many_ok(role, spec, payloads):
        batches.append(len(payloads))
        return [runner_stubs._deep_researcher_stub(p) for p in payloads]

    def run_many_one_fails(role, spec, payloads):
        batches.append(len(payloads))
        return [RuntimeError("batch item failed")] + [runner_stubs._deep_researcher_stub(p) for p in payloads[1:]]

    results = {}
    for name, many in (("single", None), ("batched", run_many_ok), ("failing", run_many_one_fails)):
        v = ObsidianManager(vault_base=tmp_path / name)
        v.cold_start_protocol()
        _shadow_setup(monkeypatch, v)
        direct.clear()
        batches.clear()
        if many is not None:
            monkeypatch.setattr(direct_runner, "run_many", many)
        assert _run_shadows(v, plain=many is None) == 2
        results[name] = (_shadow_rows(v), list(direct), list(batches))
    rows_single, direct_s, batches_s = results["single"]
    assert batches_s == [] and len(direct_s) == 2  # unbatched: one sync call per market
    rows_batched, direct_b, batches_b = results["batched"]
    rows_fail, direct_f, batches_f = results["failing"]
    assert len(rows_single) == 2 and all("p" in r and "error" not in r for r in rows_single)
    assert rows_batched == rows_single
    assert batches_b == [2] and direct_b == []
    # the failing item went through the one-by-one sync path; ledger rows still identical
    assert batches_f == [2] and len(direct_f) == 1
    assert rows_fail == rows_single
