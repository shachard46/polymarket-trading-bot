"""F5 regression: a Deep Researcher market_id mismatch DLQs the market."""

from __future__ import annotations

from typing import Any

import pytest

from obsidian_utils import ObsidianManager
from orchestrator import phases


@pytest.fixture()
def vault(tmp_path):
    return ObsidianManager(vault_base=tmp_path)


def _row() -> dict[str, Any]:
    return {
        "market_id": "0xRIGHT",
        "market_title": "Right market",
        "market_description": "desc",
        "market_data": {"yes_price": 0.5},
    }


def test_mismatched_market_id_quarantines(vault, tmp_path):
    def runner(role: str, payload: dict[str, Any]) -> Any:
        if role == "briefer":
            return {
                "market_id": payload["market_id"],
                "summary": "ok",
                "error": None,
            }
        if role == "deep_researcher":
            return (
                "---\n"
                'market_id: "0xWRONG"\n'
                "estimated_p: 0.6\n"
                "error: null\n"
                "---\n\n"
                "## Bull Thesis\n\nbody\n\n## Bear Thesis\n\nbody\n\n## Post-Mortem\n"
            )
        raise AssertionError(role)

    out = phases.phase3_qualitative_pipeline(vault, [_row()], runner=runner)
    assert out == []
    err_files = list((tmp_path / "Vault" / "05_Errors").glob("0xRIGHT*.json"))
    assert err_files, "expected DLQ artifact for mismatched market_id"
    assert "mismatched market_id" in err_files[0].read_text(encoding="utf-8")


def test_matching_market_id_proceeds(vault):
    def runner(role: str, payload: dict[str, Any]) -> Any:
        if role == "briefer":
            return {"market_id": payload["market_id"], "summary": "ok", "error": None}
        if role == "deep_researcher":
            return (
                "---\n"
                f'market_id: "{payload["market_id"]}"\n'
                "estimated_p: 0.6\n"
                "error: null\n"
                "---\n\n"
                "## Bull Thesis\n\nb\n\n## Bear Thesis\n\nb\n\n## Post-Mortem\n"
            )
        raise AssertionError(role)

    out = phases.phase3_qualitative_pipeline(vault, [_row()], runner=runner)
    assert len(out) == 1
    assert out[0]["market_id"] == "0xRIGHT"
