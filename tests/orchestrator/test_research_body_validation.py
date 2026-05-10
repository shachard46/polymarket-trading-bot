"""Deep Researcher markdown body must follow section contract."""

from __future__ import annotations

import pytest

from orchestrator.research import parse_deep_researcher


def _md(body_suffix: str = "") -> str:
    return (
        "---\n"
        'market_id: "x"\n'
        "estimated_p: 0.5\n"
        "error: null\n"
        "---\n\n"
        "## Bull Thesis\n\nb.\n\n"
        "## Bear Thesis\n\nbe.\n\n"
        "## Post-Mortem\n"
        f"{body_suffix}"
    )


def test_parse_accepts_empty_post_mortem_section():
    r = parse_deep_researcher(_md())
    assert "b." in r.body
    assert "## Post-Mortem" in r.body


def test_parse_rejects_nonempty_post_mortem_section():
    with pytest.raises(ValueError, match="Post-Mortem"):
        parse_deep_researcher(_md("oops"))
