"""Wave 4: the learning loop (failure classes, gated/bounded/versioned Overseer, rollback)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from config.trading_constants import BOUNDS, FILTERS
from obsidian_utils import ObsidianManager, VaultWriteError
from orchestrator import gate, ledger, phases, replay, scraper, stats
from orchestrator.directives import (
    DirectiveBoundsError,
    directive_version,
    effective_knobs,
    extract_filter_directives,
    validate_directive_change,
)
from tests.orchestrator.test_phase2_gate_triage import Triage, mk
from tests.orchestrator.test_research_redesign import World, _seed


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    monkeypatch.delenv("OVERSEER_MIN_NEW_RESOLVED", raising=False)
    v = ObsidianManager(vault_base=tmp_path)
    v.cold_start_protocol()
    return v


def doc(body: str = "Body.", **knobs: Any) -> str:
    """A complete directives doc with every knob at its default unless overridden."""
    values = {k: FILTERS[k] for k in BOUNDS} | knobs
    block = "\n".join(f"  {k}: {v}" for k, v in values.items())
    return (
        "---\ntitle: Active Directives\n---\n\n# Active Directives\n\n"
        f"## Research Protocol\n\n{body}\n\n## Filter Weightings\n\n```yaml\nfilters:\n{block}\n```\n\n"
        "## Risk Constraints\n\nx\n\n## Output Requirements\n\nx\n"
    )


def write(vault, markdown: str, **kw):
    return vault.write_directives({"new_directives_markdown": markdown, "rationale": "r"}, **kw)


def fm_of(vault) -> dict[str, Any]:
    from orchestrator.research import split_yaml_frontmatter_markdown

    return split_yaml_frontmatter_markdown(vault.read_directives())[0]


def add_resolutions(vault, n: int, prefix: str = "r") -> None:
    for i in range(n):
        ledger.record_resolution(vault, f"{prefix}{i}", 1, "Yes", None)


# --- versioning + history ---------------------------------------------------


def test_seed_is_integer_version_one_and_archived(vault):
    assert fm_of(vault)["version"] == 1
    assert ledger.current_directive_version(vault) == 1
    assert (vault.directives_history_dir() / "v1.md").read_text() == vault.read_directives()
    assert set(effective_knobs(vault.read_directives())) == set(BOUNDS)
    assert effective_knobs(vault.read_directives()) == {k: FILTERS[k] for k in BOUNDS}


def test_legacy_seed_version_migrates_to_one(tmp_path):
    v = ObsidianManager(vault_base=tmp_path)
    legacy = doc().replace("title: Active Directives", 'version: "0.1-seed"\ntitle: Active Directives')
    v.write_file("Vault/00_System/active_directives.md", legacy)
    assert directive_version(legacy) == 1
    assert v.cold_start_protocol() is False
    # the legacy version migrates to 1, then the crux_v1 pipeline stamp bumps it once more (new scoring cohort)
    assert fm_of(v)["version"] == 2 and fm_of(v)["research_pipeline"] == "crux_v1"
    assert (v.directives_history_dir() / "v1.md").read_text().count("migrated_from: 0.1-seed") == 1
    assert (v.directives_history_dir() / "v2.md").exists()


def test_write_increments_and_archives_each_version(vault):
    write(vault, doc(research_max_q_gap=0.36))
    write(vault, doc("New protocol.", research_max_q_gap=0.36))
    assert ledger.current_directive_version(vault) == 3
    hist = vault.directives_history_dir()
    assert sorted(p.name for p in hist.glob("v*.md")) == ["v1.md", "v2.md", "v3.md"]
    assert hist.joinpath("v3.md").read_text() == vault.read_directives()
    assert "New protocol." in vault.read_directives()
    assert "Body." in hist.joinpath("v2.md").read_text()
    assert vault.directives_history_dir().parent.joinpath("active_directives.md.bak").exists()


def test_overseer_cannot_set_bookkeeping_frontmatter(vault):
    sneaky = doc().replace("title: Active", "version: 99\nknob_last_changed: {gate_min_q: 1}\ntitle: Active")
    write(vault, sneaky)
    fm = fm_of(vault)
    assert fm["version"] == 2 and fm["knob_last_changed"] == {}


def test_ledger_rows_stamp_integer_version(vault):
    write(vault, doc(research_max_q_gap=0.36))
    row = ledger.record(vault, "m", "gate_reject", q=0.4)
    assert row["directive_version"] == 2 and isinstance(row["directive_version"], int)


# --- bounds -----------------------------------------------------------------


def test_bounds_table_has_only_gate_and_research_knobs():
    assert set(BOUNDS) == {
        "gate_min_q", "gate_max_q", "min_days_to_resolution", "max_days_to_resolution",
        "research_max_quick_width", "research_max_sample_spread", "research_max_q_gap",
    }
    assert all(b["max_step_frac"] == 0.15 and b["min"] < b["max"] for b in BOUNDS.values())
    assert all(b["min"] <= FILTERS[k] <= b["max"] for k, b in BOUNDS.items())


def test_valid_small_change_accepted():
    old = doc()
    assert validate_directive_change(old, doc(research_max_q_gap=0.36), 2) == ["research_max_q_gap"]
    # integer knobs may always move by 1
    assert validate_directive_change(old, doc(min_days_to_resolution=8), 2) == ["min_days_to_resolution"]
    assert validate_directive_change(old, old, 2) == []


@pytest.mark.parametrize(
    "old_kw, new_kw, match",
    [
        ({"gate_min_q": 0.031}, {"gate_min_q": 0.029}, "outside"),  # small step, below min
        ({}, {"research_max_q_gap": 0.5}, "outside"),
        ({}, {"research_max_q_gap": 0.30}, "exceeds"),  # 25% step, in range
        ({}, {"max_days_to_resolution": 80}, "exceeds"),
        (
            {},
            {"research_max_q_gap": 0.36, "research_max_sample_spread": 0.16, "gate_min_q": 0.055},
            "3 knobs",
        ),
        ({}, {"mystery_knob": 1}, "unknown filter keys"),
        ({}, {"LAMBDA": 0.5}, "unknown filter keys"),  # sizing is off-limits
        ({}, {"research_max_q_gap": "'0.36'"}, "not a finite number"),
        ({}, {"min_days_to_resolution": 7.5}, "integer"),
    ],
)
def test_bounds_rejections(old_kw, new_kw, match):
    old = doc(**old_kw)
    new = doc(**new_kw)
    with pytest.raises(DirectiveBoundsError, match=match):
        validate_directive_change(old, new, 2)


def test_two_knobs_ok_three_rejected(vault):
    write(vault, doc(research_max_q_gap=0.36, research_max_sample_spread=0.16))
    assert ledger.current_directive_version(vault) == 2


def test_rejection_keeps_old_directives_and_history(vault):
    before = vault.read_directives()
    with pytest.raises(VaultWriteError):
        write(vault, doc(research_max_q_gap=0.46))
    assert vault.read_directives() == before
    assert sorted(p.name for p in vault.directives_history_dir().glob("v*.md")) == ["v1.md"]


def test_cooldown_blocks_same_knob_for_three_versions(vault):
    write(vault, doc(research_max_q_gap=0.36))  # v2 changes it
    assert fm_of(vault)["knob_last_changed"] == {"research_max_q_gap": 2}
    with pytest.raises(VaultWriteError):  # v3 would be 1 version later
        write(vault, doc(research_max_q_gap=0.33))
    write(vault, doc(research_max_q_gap=0.36, gate_min_q=0.055))  # v3: other knob is fine
    with pytest.raises(VaultWriteError):  # v4: still cooling (2 versions)
        write(vault, doc(research_max_q_gap=0.33, gate_min_q=0.055))
    write(vault, doc(research_max_q_gap=0.36, gate_min_q=0.055, research_max_sample_spread=0.16))  # v4
    write(vault, doc(research_max_q_gap=0.33, gate_min_q=0.055, research_max_sample_spread=0.16))  # v5 ok
    assert ledger.current_directive_version(vault) == 5
    assert fm_of(vault)["knob_last_changed"]["research_max_q_gap"] == 5


def test_restore_is_not_bounds_checked(vault):
    write(vault, doc(research_max_q_gap=0.36))
    write(vault, doc(research_max_q_gap=0.36, gate_min_q=0.055))
    assert vault.restore_directives(1, "test") == 4  # a 2-knob jump is fine for a restore


# --- Phase 6 gating / bounds / auto-revert ----------------------------------


class Overseer:
    def __init__(self, markdown: str | None = None):
        self.markdown = markdown
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, role: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert role == "overseer"
        self.payloads.append(payload)
        return {
            "new_directives_markdown": self.markdown or payload["current_directives"],
            "rationale": "r",
            "error": None,
        }


def test_phase6_needs_min_new_resolved(vault):
    run = Overseer()
    add_resolutions(vault, 29)
    phases.phase6_macro_learning_loop(vault, runner=run)
    assert run.payloads == []  # 29 < 30
    add_resolutions(vault, 1, "extra")
    phases.phase6_macro_learning_loop(vault, runner=run)
    assert len(run.payloads) == 1
    fm = fm_of(vault)
    assert fm["last_overseer_ledger_count"] == 30 and fm["last_overseer_at"]
    # counted since the LAST run, and the 24h interval also applies
    add_resolutions(vault, 30, "more")
    phases.phase6_macro_learning_loop(vault, runner=run)
    assert len(run.payloads) == 1
    vault.patch_directives_frontmatter({"last_overseer_at": "2020-01-01T00:00:00+00:00"})
    phases.phase6_macro_learning_loop(vault, runner=run)
    assert len(run.payloads) == 2


def test_phase6_min_new_resolved_env(vault, monkeypatch):
    monkeypatch.setenv("OVERSEER_MIN_NEW_RESOLVED", "2")
    run = Overseer()
    add_resolutions(vault, 1)
    phases.phase6_macro_learning_loop(vault, runner=run)
    assert run.payloads == []
    add_resolutions(vault, 1, "x")
    phases.phase6_macro_learning_loop(vault, runner=run)
    assert len(run.payloads) == 1


def test_phase6_payload_has_stats_failure_classes_bounds_and_15_post_mortems(vault, monkeypatch):
    monkeypatch.setenv("OVERSEER_MIN_NEW_RESOLVED", "0")
    for i in range(20):
        vault.post_mortem_path(f"pm{i:02d}").write_text("x" * 9000, encoding="utf-8")
    ledger.record(vault, "a", "post_mortem", failure_class="variance")
    ledger.record(vault, "b", "post_mortem", failure_class="bad_info")
    ledger.record(vault, "c", "post_mortem", failure_class="variance")
    run = Overseer()
    phases.phase6_macro_learning_loop(vault, runner=run)
    (payload,) = run.payloads
    assert len(payload["post_mortems"]) <= 15
    assert all(len(p["content"]) <= 5 * 1024 for p in payload["post_mortems"])
    assert payload["failure_class_counts"] == {"bad_info": 1, "variance": 2}
    assert payload["stats_md"].startswith("# Stats")
    assert {b["knob"] for b in payload["bounds"]} == set(BOUNDS)
    row = next(b for b in payload["bounds"] if b["knob"] == "research_max_q_gap")
    assert row["current"] == 0.4 and row["min"] == 0.2 and row["locked"] is False


def test_phase6_applies_valid_change_and_noop_does_not_bump(vault, monkeypatch):
    monkeypatch.setenv("OVERSEER_MIN_NEW_RESOLVED", "0")
    phases.phase6_macro_learning_loop(vault, runner=Overseer())  # no change proposed
    assert ledger.current_directive_version(vault) == 1
    vault.patch_directives_frontmatter({"last_overseer_at": "2020-01-01T00:00:00+00:00"})
    phases.phase6_macro_learning_loop(vault, runner=Overseer(doc(research_max_q_gap=0.36)))
    assert ledger.current_directive_version(vault) == 2
    assert effective_knobs(vault.read_directives())["research_max_q_gap"] == 0.36


def test_phase6_rejects_whole_doc_on_bounds_violation(vault, monkeypatch, caplog):
    monkeypatch.setenv("OVERSEER_MIN_NEW_RESOLVED", "0")
    before = vault.read_directives()
    bad = doc("Rewritten protocol.", research_max_q_gap=0.46)  # also edits the protocol text
    phases.phase6_macro_learning_loop(vault, runner=Overseer(bad))
    assert vault.read_directives() == before  # protocol edit rejected with it
    assert "rejected" in caplog.text
    assert list(ledger.iter_rows(vault)) == []  # ledger nothing
    assert sorted(p.name for p in vault.directives_history_dir().glob("v*.md")) == ["v1.md"]


def _scored_rows(vault, version: int, n: int, p: float, prefix: str) -> None:
    for i in range(n):
        mid = f"{prefix}{i}"
        ledger.record(vault, mid, "below_edge", p=p, q=0.5, directive_version=version)
        ledger.record_resolution(vault, mid, 1, "Yes", None)


def test_brier_by_directive_version():
    rows = [
        {"market_id": "a", "stage": "below_edge", "p": 0.9, "q": 0.5, "directive_version": 1},
        {"market_id": "a", "stage": "resolution", "outcome_yes": 1},
        {"market_id": "b", "stage": "below_edge", "p": 0.2, "q": 0.5, "directive_version": 2},
        {"market_id": "b", "stage": "resolution", "outcome_yes": 1},
        {"market_id": "b", "stage": "post_mortem", "p": 0.99, "directive_version": 2},
    ]
    out = stats.brier_by_directive_version(rows)
    assert out[1] == (1, pytest.approx(0.01)) and out[2] == (1, pytest.approx(0.64))


def test_auto_revert_restores_previous_version_and_skips_overseer(vault, caplog):
    write(vault, doc(research_max_q_gap=0.36))  # v2
    _scored_rows(vault, 1, 30, 0.9, "good")  # Brier 0.01
    _scored_rows(vault, 2, 30, 0.6, "bad")  # Brier 0.16
    run = Overseer(doc(research_max_q_gap=0.40))
    with caplog.at_level("ERROR"):
        phases.phase6_macro_learning_loop(vault, runner=run)
    assert run.payloads == []  # Overseer skipped this run
    assert "AUTO-REVERTED" in caplog.text
    assert ledger.current_directive_version(vault) == 3
    fm = fm_of(vault)
    assert fm["restored_from"] == 1 and "auto-revert" in fm["restore_note"]
    assert effective_knobs(vault.read_directives())["research_max_q_gap"] == 0.4
    assert (vault.directives_history_dir() / "v3.md").exists()
    # the restored version is never itself auto-reverted
    phases.phase6_macro_learning_loop(vault, runner=run)
    assert ledger.current_directive_version(vault) == 3


@pytest.mark.parametrize(
    "n_cur, p_cur",
    [(29, 0.6), (30, 0.85)],  # too few samples; or Brier 0.0225 vs 0.01 (worse by < 0.02)
)
def test_no_auto_revert(vault, n_cur, p_cur):
    write(vault, doc(research_max_q_gap=0.36))
    _scored_rows(vault, 1, 30, 0.9, "good")
    _scored_rows(vault, 2, n_cur, p_cur, "cur")
    phases.phase6_macro_learning_loop(vault, runner=Overseer())
    assert ledger.current_directive_version(vault) == 2
    assert "restored_from" not in fm_of(vault)


# --- replay --directives ----------------------------------------------------


def test_replay_directives_cli_restores_as_new_version(tmp_path, capsys):
    v = ObsidianManager(vault_base=tmp_path)
    v.cold_start_protocol()
    v1_text = v.read_directives()
    write(v, doc("Second.", research_max_q_gap=0.36))
    write(v, doc("Third.", research_max_q_gap=0.36, gate_min_q=0.055))
    assert replay.main(["--directives", "1", "--vault-path", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"restored_from": 1, "new_version": 4}
    assert ledger.current_directive_version(v) == 4
    fm = fm_of(v)
    assert fm["restored_from"] == 1 and "replay --directives" in fm["restore_note"]
    assert effective_knobs(v.read_directives()) == {k: FILTERS[k] for k in BOUNDS}
    assert v.read_directives_version(4) == v.read_directives()
    assert v.read_directives_version(1) == v1_text  # the original is untouched
    with pytest.raises(SystemExit):
        replay.main(["--directives", "42", "--vault-path", str(tmp_path)])


# --- knob wiring ------------------------------------------------------------


def knobs_for(**kw) -> dict[str, Any]:
    return extract_filter_directives(doc(**kw))


def test_gate_reads_price_band_and_days_window():
    row = {"yes_price": 0.04, "no_price": 0.96, "liquidity": 20_000, "days_to_resolution": 30}
    assert gate.eligibility(row)[1] == "price_extreme"
    assert gate.eligibility(row, knobs_for(gate_min_q=0.03)) == (True, "ok")
    hi = {**row, "yes_price": 0.96}
    assert gate.eligibility(hi, knobs_for(gate_max_q=0.97)) == (True, "ok")
    soon = {**row, "yes_price": 0.4, "days_to_resolution": 5}
    assert gate.eligibility(soon)[1] == "too_soon"
    assert gate.eligibility(soon, knobs_for(min_days_to_resolution=5)) == (True, "ok")
    far = {**row, "yes_price": 0.4, "days_to_resolution": 70}
    assert gate.eligibility(far)[1] == "too_far"
    assert gate.eligibility(far, knobs_for(max_days_to_resolution=75)) == (True, "ok")
    assert gate.score({**soon, "days_to_resolution": 10}) != gate.score(
        {**soon, "days_to_resolution": 10}, knobs_for(min_days_to_resolution=9)
    )


def test_phase2_uses_directive_knobs(vault):
    market = mk("a", days_to_resolution=5)
    phases.phase2_quantitative_routing(vault, [market], runner=Triage())
    assert [r["reason"] for r in ledger.iter_rows(vault) if r["stage"] == "gate_reject"] == ["too_soon"]

    market = mk("b", days_to_resolution=5)
    write(vault, doc(min_days_to_resolution=5), enforce_bounds=False)
    phases.phase2_quantitative_routing(vault, [market], runner=Triage())
    assert vault.read_filter_log("b")["passed"] is True


def test_quick_decision_uses_width_knob():
    assert phases.quick_decision(0.3, 0.7, 0.5) == "escalate"  # width 0.4 > 0.2
    assert phases.quick_decision(0.3, 0.7, 0.5, knobs_for(research_max_quick_width=0.45)["research_max_quick_width"]) == "no_divergence"


def test_phase3_sample_spread_and_q_gap_follow_directives(monkeypatch, vault):
    # J1 0.5 vs flipped J2 0.75: framing gap 0.25 -> skipped by default, researched once the knob is 0.25
    def ran(mid: str, q: float, dr_p) -> list[dict]:
        w = World(monkeypatch, vault)
        w.market(mid, q)
        _seed(vault, mid)
        w.dr_p = dr_p
        out = phases.phase3_qualitative_pipeline(vault, runner=w.runner)
        return out

    ps = {"j1": 0.5, "j2": 0.75}
    spread_p = lambda payload: (0.6, 0.5, 0.7) if payload["mode"] == "quick" else (ps[payload["session_tag"]], None, None)  # noqa: E731
    assert ran("s_default", 0.42, spread_p) == []
    write(vault, doc(research_max_sample_spread=0.25), enforce_bounds=False)
    assert len(ran("s_loose", 0.42, spread_p)) == 1

    # |p - q| = 0.45: a misread by default (gap 0.40), fine with a 0.46 gap knob
    assert ran("g_default", 0.20, 0.65) == []
    write(vault, doc(research_max_sample_spread=0.25, research_max_q_gap=0.46), enforce_bounds=False)
    assert len(ran("g_loose", 0.20, 0.65)) == 1


# --- Phase 5: post-mortem payload + failure_class ---------------------------


def _seed_traded_market(vault, mid="0xpm") -> None:
    vault.write_trade_log(
        mid,
        {
            "market_id": mid, "allocation_usd": 10.0, "executed": False, "transaction_hash": None,
            "error": None, "p": 0.7, "p_low": 0.6, "p_high": 0.8, "q_entry": 0.4, "side": "YES",
            "price": 0.4, "shares": 25.0,
        },
    )
    vault.write_research_report(
        mid,
        {
            "market_id": mid, "estimated_p": 0.7, "p_low": 0.6, "p_high": 0.8, "error": None,
            "confidence": "high", "resolution_checklist": ["YES needs the titled event"],
            "q_at_research": 0.4,
        },
        "## Bull Thesis\n\nb\n\n## Bear Thesis\n\nb\n\n## Post-Mortem\n",
    )
    vault.write_filter_log(
        mid,
        {
            "market_id": mid, "passed": True, "trigger": None, "confidence_multiplier": 0.81,
            "score": 0.81, "details": "ok", "error": None,
            "triage": {"researchable": True, "category": "politics", "reason": "r"},
        },
    )


def _runner(captured: list, failure_class="variance"):
    def run(role: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert role == "post_mortem_analyst"
        captured.append(payload)
        return {
            "market_id": payload["market_id"], "post_mortem_analysis": "para",
            "failure_class": failure_class, "error": None,
        }

    return run


def test_post_mortem_payload_fields_and_persistence(vault, monkeypatch):
    _seed_traded_market(vault)
    ledger.record(vault, "0xpm", "traded", p=0.7, q=0.4, side="YES", price=0.4, size_usd=10.0)
    monkeypatch.setattr(
        scraper, "fetch_resolution", lambda mid: {"market_id": mid, "status": "closed", "outcome": "yes"}
    )
    seen: list = []
    phases.phase5_resolution_and_post_mortem(vault, runner=_runner(seen))
    (payload,) = seen
    assert payload["p"] == 0.7 and (payload["p_low"], payload["p_high"]) == (0.6, 0.8)
    assert payload["q_entry"] == 0.4 and payload["side"] == "YES" and payload["price"] == 0.4
    assert payload["size_usd"] == 10.0 and payload["outcome_yes"] == 1
    expected = stats.bet_pnl({"price": 0.4, "size_usd": 10.0, "side": "YES"}, 1)
    assert payload["realised_pnl_usd"] == pytest.approx(expected) == pytest.approx(15.0)  # pre-tax
    assert payload["category"] == "politics" and payload["gate_score"] == 0.81
    assert payload["research_confidence"] == "high"
    assert payload["resolution_checklist"] == ["YES needs the titled event"]
    assert "Bull Thesis" in payload["original_research"] and payload["resolution_data"]["outcome"] == "yes"

    # failure_class: post-mortem frontmatter + a ledger `post_mortem` row
    assert vault.read_market_record("0xpm", "post_mortem")["failure_class"] == "variance"
    (row,) = [r for r in ledger.iter_rows(vault) if r["stage"] == "post_mortem"]
    assert row["failure_class"] == "variance" and row["pnl_usd"] == pytest.approx(15.0)
    assert row["outcome_yes"] == 1 and row["category"] == "politics"
    assert stats.failure_class_counts(list(ledger.iter_rows(vault))) == {"variance": 1}
    # the post_mortem row does not disturb the decision bookkeeping
    assert ledger.latest_decision_by_market(vault)["0xpm"]["stage"] == "traded"


def _res(mid, outcome="yes"):
    return {"market_id": mid, "status": "closed", "outcome": outcome, "resolved_at": "2026-02-01T00:00:00+00:00"}


def test_post_mortem_payload_new_fields_null_when_missing(vault, monkeypatch):
    _seed_traded_market(vault)
    monkeypatch.setattr(scraper, "fetch_resolution", lambda mid: _res(mid))
    seen: list = []
    phases.phase5_resolution_and_post_mortem(vault, runner=_runner(seen))
    (p,) = seen
    assert p["crux_table"] is None and p["known_unknowns"] is None and p["cited_evidence"] is None
    assert p["researched_at"] is None and p["end_date"] is None
    assert p["resolved_at"] == "2026-02-01T00:00:00+00:00"


def test_post_mortem_payload_new_fields_present_and_capped(vault, monkeypatch):
    mid = "0xpm"
    vault.write_trade_log(mid, {
        "market_id": mid, "allocation_usd": 10.0, "executed": False, "transaction_hash": None,
        "error": None, "p": 0.7, "q_entry": 0.4, "side": "YES", "price": 0.4, "shares": 25.0,
    })
    bull = [f"fact {i} | https://example.com/{i} | 2026-01-0{1 + i % 9}" for i in range(60)]
    vault.write_research_report(mid, {
        "market_id": mid, "estimated_p": 0.7, "error": None, "researched_at": "2026-01-10T00:00:00+00:00",
        "end_date": "2026-02-01", "evidence_bull": bull, "evidence_bear": ["x | https://example.org/x | 2026-01-02 (unverified)"],
    }, "## Bull Thesis\n\nb\n\n## Bear Thesis\n\nb\n\n## Post-Mortem\n")
    vault.write_research_bundle(mid, [{"query": "q", "crux_id": "c1", "role": "evidence", "result": "- fact 0 | https://example.com/0 | 2026-01-01"}], {
        "crux_meta": {"cruxes": [{"crux_id": "c1", "role": "evidence", "question": "Q?", "status": "unanswered",
                                   "missing": "no poll", "followed_up": False}],
                      "known_unknowns": ["Q? (no poll)"]},
    })
    monkeypatch.setattr(scraper, "fetch_resolution", lambda mid: _res(mid))
    seen: list = []
    phases.phase5_resolution_and_post_mortem(vault, runner=_runner(seen))
    (p,) = seen
    assert p["crux_table"] == [{"crux_id": "c1", "role": "evidence", "question": "Q?", "status": "unanswered", "missing": "no poll"}]
    assert p["known_unknowns"] == ["Q? (no poll)"]
    assert p["researched_at"].startswith("2026-01-10") and p["end_date"] == "2026-02-01"
    ev = p["cited_evidence"]
    assert 0 < len(ev) <= 40 and len(str(ev)) <= 8000 + 500
    assert ev[0]["cited_for"] == "yes" and ev[0]["fact"] == "fact 0" and ev[0]["url"].startswith("https://example.com/0")


def test_post_mortem_losing_bet_pnl_and_invalid_failure_class(vault, monkeypatch):
    _seed_traded_market(vault)
    monkeypatch.setattr(
        scraper, "fetch_resolution", lambda mid: {"market_id": mid, "status": "closed", "outcome": "no"}
    )
    seen: list = []
    phases.phase5_resolution_and_post_mortem(vault, runner=_runner(seen, failure_class="unlucky"))
    assert seen[0]["outcome_yes"] == 0 and seen[0]["realised_pnl_usd"] == pytest.approx(-10.0)
    # an invalid class is stored as 'unclassified' (not a failed attempt, market not flagged)
    pm = [r for r in ledger.iter_rows(vault) if r["stage"] == "post_mortem"]
    assert [r["failure_class"] for r in pm] == ["unclassified"]
    assert (vault.read_market_record("0xpm", "post_mortem") or {}).get("failure_class") == "unclassified"


def test_post_mortem_schema_requires_failure_class_and_inputs():
    from agents_blueprint import AGENTS

    spec = AGENTS["post_mortem_analyst"]
    assert "failure_class" in spec["output_schema"]
    for key in ("p", "p_low", "p_high", "q_entry", "side", "price", "size_usd", "outcome_yes",
                "realised_pnl_usd", "category", "gate_score", "research_confidence",
                "resolution_checklist", "crux_table", "known_unknowns", "cited_evidence",
                "researched_at", "resolved_at", "end_date"):
        assert key in spec["input_schema"]
    prompt = spec["system_prompt"]
    assert "70% call that loses can be *correct*" in prompt
    for cls in ("bad_info", "bad_reasoning", "resolution_misread", "variance", "correct"):
        assert cls in prompt


def test_overseer_prompt_states_the_rules():
    from agents_blueprint import AGENTS

    prompt = AGENTS["overseer"]["system_prompt"]
    assert "at most 2 knobs" in prompt and "15%" in prompt
    assert "stat that justifies" in prompt and "may NOT touch sizing" in prompt
