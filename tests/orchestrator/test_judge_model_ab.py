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
    for k in ("PMBOT_JUDGE_MODEL_AB", "PMBOT_JUDGE_MODEL_ALT", "PMBOT_JUDGE_MODEL_ALT_SHARE"):
        monkeypatch.delenv(k, raising=False)


def test_arm_split_is_about_60_40_and_stable():
    arms = [evidence.judge_model_arm(i) for i in IDS]
    share = arms.count("deepseek") / len(arms)
    assert 0.56 < share < 0.64 and set(arms) == {"deepseek", "primary"}
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
