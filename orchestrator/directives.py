"""Parse filter threshold directives from active_directives.md."""

from __future__ import annotations

import logging
import math
import re
from typing import Any

import yaml

from config.trading_constants import (
    BOUNDS,
    FILTERS,
    KNOB_COOLDOWN_VERSIONS,
    MAX_KNOBS_CHANGED_PER_RUN,
)

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


class DirectiveBoundsError(ValueError):
    """The proposed directives violate the Overseer guard-rails (BOUNDS, step, count, cooldown)."""


def _frontmatter(markdown: str) -> dict[str, Any]:
    from orchestrator.research import split_yaml_frontmatter_markdown

    try:
        return split_yaml_frontmatter_markdown(markdown)[0] if markdown.strip() else {}
    except ValueError:
        return {}


def directive_version(markdown: str) -> int:
    """Monotonic integer ``version`` from the frontmatter.

    Missing doc / missing key -> 0. A legacy non-integer value (the old ``"0.1-seed"``) migrates
    to 1.
    """
    fm = _frontmatter(markdown)
    raw = fm.get("version")
    if raw is None:
        return 0
    if isinstance(raw, bool):
        return 1
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return 1


def effective_knobs(markdown: str) -> dict[str, float | int]:
    """Current value of every tunable knob (directive override, else the constant default)."""
    merged = extract_filter_directives(markdown)
    return {k: merged[k] for k in BOUNDS}


def changed_knobs(old_markdown: str, new_markdown: str) -> list[str]:
    old, new = effective_knobs(old_markdown), effective_knobs(new_markdown)
    return [k for k in BOUNDS if abs(float(new[k]) - float(old[k])) > 1e-12]


def validate_directive_change(old_markdown: str, new_markdown: str, new_version: int) -> list[str]:
    """Check ``new_markdown``'s ``filters:`` against ``old_markdown``; return the changed knobs.

    Raises :class:`DirectiveBoundsError` (the caller rejects the WHOLE document) on: an unknown
    key, a non-numeric / non-finite / non-integer-for-int value, a value outside ``BOUNDS``, a
    step above ``max_step_frac`` of the current value (int knobs may always move by 1), more than
    ``MAX_KNOBS_CHANGED_PER_RUN`` knobs changed, or a knob changed less than
    ``KNOB_COOLDOWN_VERSIONS`` versions ago.
    """
    filters = parse_filters_block(new_markdown)
    if filters is None:
        raise DirectiveBoundsError("no parseable filters: block")
    unknown = sorted(str(k) for k in filters if k not in BOUNDS)
    if unknown:
        raise DirectiveBoundsError(f"unknown filter keys: {unknown}")
    for key, value in filters.items():
        if value is None:
            continue  # omitted knob == default; judged below via effective_knobs
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise DirectiveBoundsError(f"{key}={value!r} is not a finite number")
        if isinstance(FILTERS[key], int) and value != int(value):
            raise DirectiveBoundsError(f"{key}={value!r} must be an integer")
        b = BOUNDS[key]
        if not b["min"] <= value <= b["max"]:
            raise DirectiveBoundsError(f"{key}={value} outside [{b['min']}, {b['max']}]")

    old, new = effective_knobs(old_markdown), effective_knobs(new_markdown)
    changed: list[str] = []
    for key in BOUNDS:
        cur, nxt = float(old[key]), float(new[key])
        if abs(nxt - cur) <= 1e-12:
            continue
        changed.append(key)
        b = BOUNDS[key]
        if not b["min"] <= nxt <= b["max"]:
            raise DirectiveBoundsError(f"{key}={nxt} outside [{b['min']}, {b['max']}]")
        allowed = b["max_step_frac"] * abs(cur) + 1e-9
        if isinstance(FILTERS[key], int):
            allowed = max(allowed, 1.0)
        if abs(nxt - cur) > allowed:
            raise DirectiveBoundsError(
                f"{key}: step {cur} -> {nxt} exceeds {b['max_step_frac']:.0%} of current"
            )
    if len(changed) > MAX_KNOBS_CHANGED_PER_RUN:
        raise DirectiveBoundsError(
            f"{len(changed)} knobs changed ({changed}); max {MAX_KNOBS_CHANGED_PER_RUN} per run"
        )
    last_changed = _frontmatter(old_markdown).get("knob_last_changed")
    last_changed = last_changed if isinstance(last_changed, dict) else {}
    for key in changed:
        try:
            at = int(last_changed.get(key))
        except (TypeError, ValueError):
            continue
        if new_version - at < KNOB_COOLDOWN_VERSIONS:
            raise DirectiveBoundsError(
                f"{key} changed at v{at}; cooldown is {KNOB_COOLDOWN_VERSIONS} versions "
                f"(earliest v{at + KNOB_COOLDOWN_VERSIONS})"
            )
    return changed


__all__ = [
    "DirectiveBoundsError",
    "changed_knobs",
    "directive_version",
    "effective_knobs",
    "extract_filter_directives",
    "parse_filters_block",
    "validate_directive_change",
]
