"""Parse filter threshold directives from active_directives.md."""

from __future__ import annotations

import logging
import math
import re
from typing import Any

import yaml

from config.trading_constants import FILTERS

log = logging.getLogger(__name__)

_FILTER_WEIGHTINGS_HEADER = "## Filter Weightings"


def parse_filters_block(markdown: str) -> dict[str, Any] | None:
    """Return the ``filters:`` mapping from the Filter Weightings yaml block, else ``None``."""
    header_idx = markdown.find(_FILTER_WEIGHTINGS_HEADER)
    if header_idx < 0:
        return None
    section = markdown[header_idx:]
    fence_match = re.search(r"```yaml\s*\n(.*?)```", section, re.DOTALL | re.IGNORECASE)
    if not fence_match:
        return None
    try:
        parsed = yaml.safe_load(fence_match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    filters = parsed.get("filters")
    return filters if isinstance(filters, dict) else None


def _coerce_override(key: str, value: Any, default: Any) -> Any:
    """Coerce ``value`` to the type of ``default``; raise ValueError if unusable."""
    if isinstance(value, bool) or isinstance(default, bool):
        if isinstance(default, bool) and isinstance(value, bool):
            return value
        raise ValueError("bool/non-bool mismatch")
    if isinstance(default, int):
        number = float(value)
        if not math.isfinite(number) or number != int(number):
            raise ValueError("not a finite integer")
        coerced: Any = int(number)
    elif isinstance(default, float):
        coerced = float(value)
        if not math.isfinite(coerced):
            raise ValueError("non-finite")
    else:
        return value
    if coerced < 0:
        raise ValueError("negative")
    return coerced


def extract_filter_directives(markdown: str) -> dict[str, Any]:
    """Return filter thresholds from the Filter Weightings YAML block.

    Falls back to ``config.trading_constants.FILTERS`` when the section or
    ``filters:`` mapping is missing or unparsable. Each override is coerced to
    the type of its default; invalid, negative, or non-finite values are
    dropped (and logged).
    """
    if not markdown.strip():
        return dict(FILTERS)

    filters = parse_filters_block(markdown)
    if filters is None:
        return dict(FILTERS)

    merged = dict(FILTERS)
    for key, value in filters.items():
        if key in merged and value is not None:
            try:
                merged[key] = _coerce_override(key, value, FILTERS[key])
            except (TypeError, ValueError, OverflowError) as exc:
                log.warning("Dropping invalid filter override %s=%r: %s", key, value, exc)
    return merged


__all__ = ["extract_filter_directives", "parse_filters_block"]
