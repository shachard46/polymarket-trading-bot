"""Deep Researcher markdown parsing — frontmatter split + ``estimated_p`` extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml

_FRONTMATTER_RE = re.compile(r"^---\s*\n(?P<fm>.*?)\n---\s*\n?(?P<body>.*)$", re.DOTALL)


@dataclass(frozen=True)
class ParsedResearch:
    """Structured view over a Deep Researcher markdown response."""

    market_id: str | None
    estimated_p: float
    error: str | None
    body: str
    frontmatter: dict[str, Any]


def split_yaml_frontmatter_markdown(markdown: str) -> tuple[dict[str, Any], str]:
    """Split ``---`` YAML frontmatter from the following markdown body.

    Generic helper for filter logs, research reports, directives, etc.
    """
    match = _FRONTMATTER_RE.match(markdown.strip())
    if not match:
        raise ValueError("missing or invalid YAML frontmatter")
    try:
        fm_raw = yaml.safe_load(match.group("fm"))
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML frontmatter: {e}") from e
    if not isinstance(fm_raw, dict):
        raise ValueError("frontmatter must parse to a mapping")
    # YAML 1.1 may read ids like 0x1f / 123 as ints; ids are always strings.
    if fm_raw.get("market_id") is not None and not isinstance(fm_raw["market_id"], str):
        fm_raw["market_id"] = str(fm_raw["market_id"])
    return fm_raw, match.group("body").strip()


def parse_deep_researcher_frontmatter(markdown: str) -> tuple[dict[str, Any], str]:
    """Split a Deep Researcher markdown response into ``(frontmatter, body)``.

    Alias for :func:`split_yaml_frontmatter_markdown` (same wire format).
    """
    return split_yaml_frontmatter_markdown(markdown)


def parse_estimated_p_from_deep_researcher_frontmatter(markdown: str) -> float:
    """Extract and validate ``estimated_p`` from a Deep Researcher response.

    The function is deliberately narrow: it only cares about the first YAML
    frontmatter block and the ``estimated_p`` key. Numeric strings such as
    ``"0.42"`` are coerced to ``float``. ``ValueError`` is raised for missing,
    non-numeric, or out-of-range values.
    """
    frontmatter, _ = parse_deep_researcher_frontmatter(markdown)
    return _parse_estimated_p(frontmatter)


def _parse_estimated_p(frontmatter: dict[str, Any]) -> float:
    """Extract and validate ``estimated_p`` from parsed frontmatter."""
    if "estimated_p" not in frontmatter:
        raise ValueError("frontmatter is missing 'estimated_p'")

    raw = frontmatter["estimated_p"]
    if isinstance(raw, bool) or raw is None:
        raise ValueError(f"'estimated_p' must be numeric, got {raw!r}")
    try:
        p = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"'estimated_p' is not numeric: {raw!r}") from exc

    if not (0.0 <= p <= 1.0):
        raise ValueError(f"'estimated_p' must be in [0.0, 1.0], got {p}")
    return p


def _validate_deep_researcher_body(body: str) -> None:
    """Require Bull / Bear / Post-Mortem sections in order; Post-Mortem body empty."""
    bull_h = "## Bull Thesis"
    bear_h = "## Bear Thesis"
    pm_h = "## Post-Mortem"
    i_bull = body.find(bull_h)
    i_bear = body.find(bear_h)
    i_pm = body.find(pm_h)
    if i_bull < 0 or i_bear < 0 or i_pm < 0:
        raise ValueError(
            "deep researcher body must contain ## Bull Thesis, ## Bear Thesis, and ## Post-Mortem"
        )
    if not (i_bull < i_bear < i_pm):
        raise ValueError(
            "deep researcher sections must appear in order: Bull, Bear, Post-Mortem"
        )
    bull_text = body[i_bull + len(bull_h) : i_bear].strip()
    bear_text = body[i_bear + len(bear_h) : i_pm].strip()
    if not bull_text:
        raise ValueError("## Bull Thesis must have non-empty content")
    if not bear_text:
        raise ValueError("## Bear Thesis must have non-empty content")
    tail = body[i_pm + len(pm_h) :].strip()
    if tail:
        raise ValueError(
            "## Post-Mortem section must be empty (no text after header; reserved for analyst)"
        )


def parse_deep_researcher(markdown: str) -> ParsedResearch:
    """Parse a complete Deep Researcher response into :class:`ParsedResearch`."""
    frontmatter, body = parse_deep_researcher_frontmatter(markdown)
    market_id = frontmatter.get("market_id")
    if market_id is not None:
        market_id = str(market_id)

    err = frontmatter.get("error")
    if err is not None:
        err = str(err).strip() or None

    if err:
        # Agent-reported failure: surface it before validating body / estimated_p.
        return ParsedResearch(
            market_id=market_id,
            estimated_p=0.0,
            error=err,
            body=body,
            frontmatter=frontmatter,
        )

    _validate_deep_researcher_body(body)
    p = _parse_estimated_p(frontmatter)

    return ParsedResearch(
        market_id=market_id,
        estimated_p=p,
        error=err,
        body=body,
        frontmatter=frontmatter,
    )


__all__ = [
    "ParsedResearch",
    "split_yaml_frontmatter_markdown",
    "parse_deep_researcher_frontmatter",
    "parse_estimated_p_from_deep_researcher_frontmatter",
    "parse_deep_researcher",
]
