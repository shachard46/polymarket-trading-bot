"""F6 regression: repeated quarantines never overwrite prior DLQ entries."""

from __future__ import annotations

from obsidian_utils import ObsidianManager
from orchestrator.dead_letter import quarantine_market


def test_repeated_quarantines_keep_distinct_artifacts(tmp_path):
    vault = ObsidianManager(vault_base=tmp_path)
    quarantine_market(vault, "0xabc", "first failure", {"x": 1})
    quarantine_market(vault, "0xabc", "second failure", {"x": 2})
    quarantine_market(vault, "0xabc", "third failure", {"x": 3})

    files = vault.iter_error_logs("0xabc")
    assert len(files) == 3, f"expected 3 distinct DLQ artifacts, got {files}"
    contents = [f.read_text(encoding="utf-8") for f in files]
    assert any("first failure" in c for c in contents)
    assert any("second failure" in c for c in contents)
    assert any("third failure" in c for c in contents)


def test_move_to_errors_does_not_overwrite_existing_artifact(tmp_path):
    vault = ObsidianManager(vault_base=tmp_path)
    # Prime the errors dir with a vanilla {market_id}.md (e.g. from a prior
    # quarantine that moved the active research file).
    (vault._dirs["errors"] / "0xabc.md").write_text("first", encoding="utf-8")
    # And drop a fresh active research file with the same id.
    (vault._dirs["active"] / "0xabc.md").write_text("second", encoding="utf-8")

    vault.move_file("0xabc", "active", "errors")

    files = sorted((vault._dirs["errors"]).glob("0xabc*.md"))
    assert len(files) == 2
    contents = {f.read_text(encoding="utf-8") for f in files}
    assert {"first", "second"} == contents
