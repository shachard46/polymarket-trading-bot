"""Parse filter threshold directives from active_directives.md."""

from __future__ import annotations

import re
from typing import Any

import yaml

from config.trading_constants import FILTERS

_FILTER_WEIGHTINGS_HEADER = "## Filter Weightings"


def extract_filter_directives(markdown: str) -> dict[str, Any]:
    """Return filter thresholds from the Filter Weightings YAML block.

    Falls back to ``config.trading_constants.FILTERS`` when the section or
    ``filters:`` mapping is missing or unparsable.
    """
    if not markdown.strip():
        return dict(FILTERS)

    header_idx = markdown.find(_FILTER_WEIGHTINGS_HEADER)
    if header_idx < 0:
        return dict(FILTERS)

    section = markdown[header_idx:]
    fence_match = re.search(r"```yaml\s*\n(.*?)```", section, re.DOTALL | re.IGNORECASE)
    if not fence_match:
        return dict(FILTERS)

    try:
        parsed = yaml.safe_load(fence_match.group(1))
    except yaml.YAMLError:
        return dict(FILTERS)

    if not isinstance(parsed, dict):
        return dict(FILTERS)

    filters = parsed.get("filters")
    if not isinstance(filters, dict):
        return dict(FILTERS)

    merged = dict(FILTERS)
    for key, value in filters.items():
        if key in merged and value is not None:
            merged[key] = value
    return merged


__all__ = ["extract_filter_directives"]
