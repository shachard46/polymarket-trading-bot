"""Judge MODEL A/B (deepseek vs the models.yaml judge): arm hash, per-call override, fallback, ledger, stats. No network."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from orchestrator import config, evidence, judge_model
from orchestrator.llm import routing
from orchestrator.phases import p3_research
from orchestrator.phases.agent_call import invoke_deep_researcher
from orchestrator.phases.common import _ledger_market_fields
from orchestrator.stats import compute_stats, render_stats
from tests.orchestrator.test_crux_pipeline import ROW, TODAY, fx  # noqa: F401

ALT = "openrouter:deepseek/deepseek-v4.1-flash"
IDS = [f"0x{i:04x}" for i in range(2000)]


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    for k in ("PMBOT_JUDGE_MODEL_AB", "PMBOT_JUDGE_MODEL_ALT", "PMBOT_JUDGE_MODEL_ALT_SHARE",
              "PMBOT_JUDGE_MODEL_SONNET", "PMBOT_JUDGE_MODEL_SONNET_SHARE"):
        monkeypatch.delenv(k, raising=False)


def test_arm_split_is_about_60_30_10_and_stable():
    arms = [evidence.judge_model_arm(i) for i in IDS]
    n = len(arms)
    assert 0.56 < arms.count("deepseek") / n < 0.64 and 0.26 < arms.count("sonnet") / n < 0.34
    assert 0.07 < arms.count("primary") / n < 0.13 and set(arms) == {"deepseek", "sonnet", "primary"}
    assert arms == [evidence.judge_model_arm(i) for i in IDS]


def test_arm_is_independent_of_other_arms():
    for other in (lambda i: evidence.ab_arm(i), lambda i: evidence.judge_framing(i), lambda i: evidence.is_shadow(i),
                  lambda i: evidence._hash_parity("judge_arm", i)):
        for val in set(other(i) for i in IDS):
            sub = [evidence.judge_model_arm(i) for i in IDS if other(i) == val]
            if len(sub) > 100:
                assert 0.5 < sub.count("deepseek") / len(sub) < 0.7
    from config.trading_constants import JUDGE_MODEL_ARM_SALT
    assert JUDGE_MODEL_ARM_SALT not in ("shadow", "judge_arm", "judge_framing")
    d = hashlib.sha256(("judge_model_arm:" + IDS[0]).encode()).hexdigest()
    assert (int(d[:8], 16) / 2**32 < 0.6) == (evidence.judge_model_arm(IDS[0]) == "deepseek")


def test_env_off_share_and_validation(monkeypatch):
    monkeypatch.setenv("PMBOT_JUDGE_MODEL_AB", "off")
    assert {evidence.judge_model_arm(i) for i in IDS[:200]} == {"primary"}
    monkeypatch.setenv("PMBOT_JUDGE_MODEL_AB", "on")
    monkeypatch.setenv("PMBOT_JUDGE_MODEL_ALT_SHARE", "1")
    monkeypatch.setenv("PMBOT_JUDGE_MODEL_SONNET_SHARE", "0")
    assert {evidence.judge_model_arm(i) for i in IDS[:200]} == {"deepseek"}
    monkeypatch.setenv("PMBOT_JUDGE_MODEL_ALT_SHARE", "1.5")
    with pytest.raises(ValueError):
        evidence.judge_model_arm("x")
    monkeypatch.setenv("PMBOT_JUDGE_MODEL_ALT_SHARE", "0.6")
    monkeypatch.setenv("PMBOT_JUDGE_MODEL_AB", "maybe")
    with pytest.raises(ValueError):
        evidence.judge_model_arm("x")
    assert config.judge_model_alt() == ALT


def _market(arm: str) -> str:
    return next(i for i in IDS if evidence.judge_model_arm(i) == arm)


def _runner(seen: list, fail_alt: bool = False):
    def run(role: str, payload: dict[str, Any]) -> Any:
        ov = (routing._overrides.get() or {}).get(role)
        seen.append((role, ov))
        if fail_alt and ov:
            raise RuntimeError("alt model down")
        return _stub(payload)
    return run


def _stub(payload):
    from orchestrator.runner_stubs import _deep_researcher_stub
    return _deep_researcher_stub(payload)


def _dr_in(mid: str) -> dict[str, Any]:
    return {"market_id": mid, "market_title": "t", "market_description": "d", "mode": "quick",
            "system_override": "x", "session_tag": "quick"}


def test_override_reaches_runner_only_for_deepseek_arm_and_deep_researcher():
    ds, pr = _market("deepseek"), _market("primary")
    seen: list = []
    invoke_deep_researcher(_runner(seen), _dr_in(ds))
    assert seen == [("deep_researcher", ALT)]
    seen.clear()
    invoke_deep_researcher(_runner(seen), _dr_in(pr))
    assert seen == [("deep_researcher", None)]
    assert routing._overrides.get() is None  # scope closed
    # the override only affects its role
    base_ws = routing.spec_for("web_searcher")
    with routing.model_override("deep_researcher", ALT):
        assert routing.spec_for("deep_researcher").model == "deepseek/deepseek-v4.1-flash"
        assert routing.spec_for("web_searcher") == base_ws  # other roles untouched
    assert routing.spec_for("deep_researcher").model != "deepseek/deepseek-v4.1-flash"


def test_fallback_to_primary_flags_and_stays_measurable(monkeypatch):
    ds = _market("deepseek")
    seen: list = []
    judge_model.reset(ds)
    out = invoke_deep_researcher(_runner(seen, fail_alt=True), _dr_in(ds))
    assert out is not None
    assert seen == [("deep_researcher", ALT), ("deep_researcher", None)]
    assert judge_model.fell_back(ds)
    monkeypatch.setenv(config.RUNNER_MODE_ENV, "live")
    f = _ledger_market_fields({}, role="deep_researcher", market_id=ds)
    spec = routing.spec_for("deep_researcher")
    assert f["judge_model_arm"] == "deepseek" and (f["provider"], f["model"]) == (spec.provider, spec.model)
    # explicit fallback state (Phase 4 reads it from pipeline_flags)
    judge_model.reset(ds)
    assert _ledger_market_fields({}, role="deep_researcher", market_id=ds)["model"] == "deepseek/deepseek-v4.1-flash"
    spec = routing.spec_for("deep_researcher")
    f = _ledger_market_fields({}, role="deep_researcher", market_id=ds, judge_fallback=True)
    assert (f["provider"], f["model"]) == (spec.provider, spec.model)


def test_ledger_fields_provider_model_reflect_override(monkeypatch):
    ds, pr = _market("deepseek"), _market("primary")
    monkeypatch.setenv(config.RUNNER_MODE_ENV, "live")
    f = _ledger_market_fields({}, role="deep_researcher", market_id=ds)
    assert (f["judge_model_arm"], f["provider"], f["model"]) == ("deepseek", "openrouter", "deepseek/deepseek-v4.1-flash")
    f = _ledger_market_fields({}, role="deep_researcher", market_id=pr)
    spec = routing.spec_for("deep_researcher")
    assert (f["judge_model_arm"], f["provider"], f["model"]) == ("primary", spec.provider, spec.model)
    monkeypatch.setenv(config.RUNNER_MODE_ENV, "stub")
    f = _ledger_market_fields({}, role="deep_researcher", market_id=ds)
    assert f["judge_model_arm"] == "deepseek" and "model" not in f


def test_log_line(caplog):
    import logging
    ds = _market("deepseek")
    with caplog.at_level(logging.INFO):
        judge_model.log_arm(ds)
    assert f"[PHASE 3] judge model arm for {ds}: deepseek (" in caplog.text


def test_run_judges_logs_and_calls_use_arm(fx, monkeypatch, caplog):  # noqa: F811
    import logging
    p3_research.build_evidence(fx.vault, fx.runner, ROW, TODAY, "full")
    meta = fx.vault.read_research_bundle_meta("m1")["crux_meta"]
    with caplog.at_level(logging.INFO):
        samples, failed = p3_research.run_judges(fx.vault, fx.runner, ROW, "d", TODAY, meta)
    assert samples and "judge model arm for m1:" in caplog.text


def test_stats_brier_by_judge_model_arm_and_render():
    rows = []
    for m, arm, p, y in [("a", "deepseek", 0.8, 1), ("b", "deepseek", 0.3, 0), ("c", "primary", 0.6, 1)]:
        rows.append({"ts": "t", "market_id": m, "stage": "traded", "p": p, "q": 0.5, "judge_model_arm": arm,
                     "pipeline": "crux_v1", "model": "x", "pipeline_flags": ["judge_model_fallback"] if m == "b" else []})
        rows.append({"ts": "t", "market_id": m, "stage": "resolution", "outcome_yes": y})
    use = [{"market_id": "a", "judge_arm": "one", "cost_usd": 0.02, "role": "deep_researcher"},
           {"market_id": "b", "judge_arm": "one", "cost_usd": 0.04, "role": "deep_researcher"},
           {"market_id": "c", "judge_arm": "two", "cost_usd": 0.10, "role": "deep_researcher"},
           {"market_id": "c", "judge_arm": None, "cost_usd": 1.0, "role": "briefer"}]
    s = compute_stats(rows, use)
    assert s["brier_by_judge_model_arm"]["deepseek"][0] == 2 and s["brier_by_judge_model_arm"]["primary"][0] == 1
    assert s["researched_by_judge_model_arm"] == {"deepseek": (2, 1), "primary": (1, 0)}
    n, total, per = s["cost"]["judge_model_arm_cost"]["deepseek"]
    assert n == 2 and total == pytest.approx(0.06) and per == pytest.approx(0.03)
    md = render_stats(s)
    assert "### Brier by judge model arm" in md and "| deepseek | 2 | 1 | 2 |" in md


# ---------------------------------------------------------------- sonnet arm (judge + web researcher)

SONNET = "claude_code:claude-sonnet-5-5"


def test_bands_and_deepseek_identical_to_old_rule():
    from config.trading_constants import JUDGE_MODEL_ARM_SALT

    for i in IDS:
        d = hashlib.sha256((JUDGE_MODEL_ARM_SALT + ":" + i).encode()).hexdigest()
        u = int(d[:8], 16) / 2**32
        want = "deepseek" if u < 0.6 else "sonnet" if u < 0.9 else "primary"
        assert evidence.judge_model_arm(i) == want
        assert (evidence.judge_model_arm(i) == "deepseek") == (u < 0.6)  # the old rule
    assert evidence.JUDGE_MODEL_SONNET == "sonnet"


def test_sonnet_share_env_and_sum_validation(monkeypatch):
    assert config.judge_model_sonnet() == SONNET and config.judge_model_sonnet_share() == 0.3
    monkeypatch.setenv("PMBOT_JUDGE_MODEL_SONNET", "claude_code:other")
    assert config.judge_model_sonnet() == "claude_code:other"
    monkeypatch.setenv("PMBOT_JUDGE_MODEL_SONNET_SHARE", "0.5")  # 0.6 + 0.5 > 1
    with pytest.raises(ValueError):
        evidence.judge_model_arm("x")
    monkeypatch.setenv("PMBOT_JUDGE_MODEL_SONNET_SHARE", "abc")
    with pytest.raises(ValueError):
        config.judge_model_sonnet_share()
    monkeypatch.setenv("PMBOT_JUDGE_MODEL_SONNET_SHARE", "0.4")  # exactly 1
    assert {evidence.judge_model_arm(i) for i in IDS[:300]} == {"deepseek", "sonnet"}
    monkeypatch.setenv("PMBOT_JUDGE_MODEL_AB", "off")
    assert {evidence.judge_model_arm(i) for i in IDS[:200]} == {"primary"}


def test_override_drops_temperature_and_carries_extras(monkeypatch, tmp_path):
    cfg = tmp_path / "models.yaml"
    cfg.write_text("default: {provider: openrouter, model: m}\nroles:\n"
                   "  web_searcher: {provider: openrouter, model: w, temperature: 0, reasoning: false}\n")
    monkeypatch.setenv("LLM_MODELS_CONFIG", str(cfg))
    assert routing.spec_for("web_searcher").temperature == 0  # yaml role
    with routing.model_override("web_searcher", SONNET, effort="medium", web_max_uses=3):
        spec = routing.spec_for("web_searcher")
        assert (spec.provider, spec.model, spec.effort, spec.web_max_uses) == ("claude_code", "claude-sonnet-5-5", "medium", 3)
        assert spec.temperature is None and spec.reasoning is None
        llm = routing.llm_for("web_searcher")
        assert llm.effort == "medium" and llm.temperature is None and llm.web_max_uses == 3
    with routing.model_override("deep_researcher", SONNET):
        spec = routing.spec_for("deep_researcher")
        assert spec.temperature is None and spec.effort is None
    assert routing.spec_for("web_searcher").temperature == 0 and routing.spec_for("web_searcher").effort is None
    assert routing.llm_for("web_searcher").effort is None  # the cache key includes effort


def test_judge_routes_sonnet_arm_and_fallback_flag():
    sn = _market("sonnet")
    seen: list = []
    judge_model.reset(sn)
    invoke_deep_researcher(_runner(seen), _dr_in(sn))
    assert seen == [("deep_researcher", SONNET)] and not judge_model.fell_back(sn)
    seen.clear()
    invoke_deep_researcher(_runner(seen, fail_alt=True), _dr_in(sn))
    assert seen == [("deep_researcher", SONNET), ("deep_researcher", None)] and judge_model.fell_back(sn)
    judge_model.reset(sn)


def test_provider_model_for_sonnet_arm(monkeypatch):
    sn = _market("sonnet")
    monkeypatch.setenv(config.RUNNER_MODE_ENV, "live")
    judge_model.reset(sn)
    assert judge_model.provider_model(sn) == ("claude_code", "claude-sonnet-5-5")
    spec = routing.spec_for("deep_researcher")
    assert judge_model.provider_model(sn, True) == (spec.provider, spec.model)


def _web_runner(seen, fail=False):
    def run(role, payload):
        ov = (routing._overrides.get() or {}).get(role)
        seen.append(ov)
        if fail and ov:
            raise RuntimeError("sonnet web down")
        q = payload["queries"][0]
        return {"results": [{"query": q, "research_data": "- fact | https://example.com/a | 2026-01-01", "error": None}]}
    return run


def test_sonnet_web_override_used_in_live_mode_and_recorded(monkeypatch):
    sn = _market("sonnet")
    monkeypatch.setenv(config.RUNNER_MODE_ENV, "live")
    seen: list = []
    rows = evidence.fetch_web_rows(_web_runner(seen), sn, ["q1", "q2"], "2026-01-01")
    assert len(rows) == 2 and all(r["research_data"] for r in rows)
    assert seen == [{"model": SONNET, "effort": "medium", "web_max_uses": 3}] * 2
    assert judge_model.web_model(sn) == SONNET and not judge_model.web_fell_back(sn)
    assert _ledger_market_fields({}, role="deep_researcher", market_id=sn)["web_model"] == SONNET
    monkeypatch.setenv(config.RUNNER_MODE_ENV, "stub")
    assert _ledger_market_fields({}, role="deep_researcher", market_id=sn)["web_model"] is None


def test_sonnet_web_failure_falls_back_and_flags(monkeypatch):
    sn = _market("sonnet")
    monkeypatch.setenv(config.RUNNER_MODE_ENV, "live")
    seen: list = []
    rows = evidence.fetch_web_rows(_web_runner(seen, fail=True), sn, ["q1"], "2026-01-01")
    assert seen[0] is not None and seen[-1] is None  # sonnet tried, then the normal path
    assert judge_model.web_fell_back(sn) and judge_model.web_model(sn) is None and len(rows) == 1


def test_non_sonnet_arm_and_stub_mode_skip_sonnet_web(monkeypatch):
    ds, sn = _market("deepseek"), _market("sonnet")
    monkeypatch.setenv(config.RUNNER_MODE_ENV, "live")
    seen: list = []
    evidence.fetch_web_rows(_web_runner(seen), ds, ["q"], "2026-01-01")
    assert seen == [None]
    monkeypatch.setenv(config.RUNNER_MODE_ENV, "stub")
    seen.clear()
    evidence.fetch_web_rows(_web_runner(seen), sn, ["q"], "2026-01-01")
    assert seen == [None]


def test_validate_checks_oauth_token_for_sonnet_arm(monkeypatch, tmp_path):
    import sys
    import types

    from orchestrator.llm.base import LLMError

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", types.ModuleType("claude_agent_sdk"))
    cfg = tmp_path / "models.yaml"
    cfg.write_text("default: {provider: openrouter, model: m}\n")
    monkeypatch.setenv("LLM_MODELS_CONFIG", str(cfg))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    with pytest.raises(LLMError, match="CLAUDE_CODE_OAUTH_TOKEN"):
        routing.validate()
    monkeypatch.setenv("PMBOT_JUDGE_MODEL_SONNET_SHARE", "0")
    routing.validate()
    monkeypatch.setenv("PMBOT_JUDGE_MODEL_SONNET_SHARE", "0.2")
    monkeypatch.setenv("PMBOT_JUDGE_MODEL_AB", "off")
    routing.validate()
    monkeypatch.setenv("PMBOT_JUDGE_MODEL_AB", "on")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    routing.validate()


def test_p3_queue_triage_shadow_batches_primary_arm_only(tmp_path, monkeypatch):
    from obsidian_utils import ObsidianManager
    from orchestrator.phases import p3_queue
    from tests.orchestrator.test_llm_batch import _run_shadows, _shadow_setup

    vault = ObsidianManager(vault_base=tmp_path)
    vault.cold_start_protocol()
    ids = _shadow_setup(monkeypatch, vault)
    arms = {ids[0]: "primary", ids[1]: "sonnet"}
    monkeypatch.setattr(judge_model, "arm_for", lambda m: arms[m])
    batched: list = []

    def fake_prefetch(runner, role, payloads):
        batched.extend(p["market_id"] for p in payloads)
        return runner

    monkeypatch.setattr(p3_queue, "prefetch_runner", fake_prefetch)
    _run_shadows(vault, plain=True)
    assert batched == [ids[0]]
