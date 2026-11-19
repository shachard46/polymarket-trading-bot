"""LLM usage rows, cost maths and the stats.md 'LLM cost' section."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from orchestrator.llm import usage
from orchestrator.llm.base import LLMResponse
from orchestrator.stats import compute_stats, render_stats

PRICES = {"batch_discount": 0.5, "providers": {"anthropic": {"m1": {"input": 3.0, "output": 15.0}}}}


def _resp(model="m1", i=1_000_000, o=100_000):
    return LLMResponse(text="x", provider="anthropic", model=model, input_tokens=i, output_tokens=o)


def _rows(tmp_path):
    return [json.loads(x) for x in (tmp_path / "Vault/05_Ledger/llm_usage.jsonl").read_text().splitlines()]


def test_cost_maths_and_batch_discount():
    assert usage.compute_cost("anthropic", "m1", 1_000_000, 100_000, prices=PRICES) == 3.0 + 1.5
    assert usage.compute_cost("anthropic", "m1", 1_000_000, 100_000, batch=True, prices=PRICES) == 2.25


def test_missing_price_is_null():
    assert usage.compute_cost("anthropic", "other", 10, 10, prices=PRICES) is None
    assert usage.compute_cost("openrouter", "m1", 10, 10, prices=PRICES) is None


def test_record_writes_row_with_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("PMBOT_VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(usage, "load_prices", lambda path=None: PRICES)
    usage.record("triage", _resp())
    with usage.market_scope("0xabc"):
        with usage.market_scope("0xdef"):
            assert usage.current_market() == "0xdef"
        usage.record("briefer", _resp(), batch=True)
    assert usage.current_market() is None
    r0, r1 = _rows(tmp_path)
    assert r0["market_id"] is None and r0["cost_usd"] == 4.5 and r0["batch"] is False
    assert r1["market_id"] == "0xabc" and r1["cost_usd"] == 2.25 and r1["batch"] is True
    assert set(r1) == {"ts", "role", "provider", "model", "market_id", "judge_arm", "input_tokens", "output_tokens", "thinking_tokens", "batch", "cost_usd"}


def test_record_never_raises(tmp_path, monkeypatch):
    f = tmp_path / "blocker"
    f.write_text("x")
    monkeypatch.setenv("PMBOT_VAULT_PATH", str(f))  # parent is a file: mkdir fails
    usage.record("triage", _resp())
    usage.record("triage", None)  # type: ignore[arg-type]


def test_stats_section_renders():
    now = datetime.now(tz=timezone.utc).isoformat()
    rows = [
        {"stage": "traded", "market_id": "m", "p": 0.8, "q": 0.5, "model": "m1"},
        {"stage": "resolution", "market_id": "m", "outcome_yes": 1},
    ]
    use = [
        {"ts": now, "role": "triage", "model": "m1", "market_id": "m", "cost_usd": 0.25},
        {"ts": now, "role": "briefer", "model": "nopriced", "market_id": "m", "cost_usd": None},
    ]
    md = render_stats(compute_stats(rows, use))
    assert "## LLM cost" in md
    assert "| m1 | 0.2500 |" in md
    assert "Rows with unknown price: 1." in md
    assert "price missing for nopriced" in md
    assert "| m1 | 1 |" in md  # Brier per dollar row
    assert "No LLM usage rows yet." in render_stats(compute_stats(rows))
