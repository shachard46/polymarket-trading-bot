"""Parse filter threshold directives from active_directives.md."""

from __future__ import annotations

import difflib
import logging
import math
from datetime import datetime, timezone
import re
from typing import Any

import yaml

from config.trading_constants import (
    BOUNDS,
    DIRECTIVE_TEXT_FORBIDDEN_PATTERNS,
    DIRECTIVE_TEXT_MAX_CHARS,
    DIRECTIVE_TEXT_MIN_SIMILARITY,
    DIRECTIVE_TEXT_RATIO_MIN_LEN,
    DIRECTIVE_TEXT_SECTIONS,
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


# ---------------------------------------------------------------------------
# Text sections: parsing, guard, rendering of the structured Overseer output
# ---------------------------------------------------------------------------

_SECTION_KEYS = {
    "Research Protocol": "research_protocol",
    "Risk Constraints": "risk_constraints",
    "Output Requirements": "output_requirements",
}


def directive_sections(markdown: str) -> dict[str, str]:
    """Level-2 sections of the body, ``{header text: stripped content}`` (fence-aware).

    A ``## `` line inside a fenced code block is content, not a header.
    """
    from orchestrator.research import split_yaml_frontmatter_markdown

    try:
        body = split_yaml_frontmatter_markdown(markdown)[1] if markdown.strip() else ""
    except ValueError:
        return {}
    sections: dict[str, list[str]] = {}
    current: str | None = None
    in_fence = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def _headers_outside_fences(text: str) -> bool:
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and re.match(r"#{1,2}\s", line):
            return True
    return False


def validate_directive_text(old_markdown: str, new_markdown: str) -> None:
    """Deterministic guard for the free-text sections; raises :class:`DirectiveBoundsError`.

    Per text section: max length, no URL / prediction-market talk / instruction-override phrase,
    no level 1-2 header (it would break the document structure), and a bounded change versus the
    current section (difflib ratio; only for new sections longer than ``DIRECTIVE_TEXT_RATIO_MIN_LEN``).
    """
    old_s, new_s = directive_sections(old_markdown), directive_sections(new_markdown)
    extra = [k for k in new_s if k not in DIRECTIVE_TEXT_SECTIONS and not k.startswith("Filter Weightings")]
    if extra:
        raise DirectiveBoundsError(f"unexpected sections (unchecked text): {extra}")
    for name in DIRECTIVE_TEXT_SECTIONS:
        new_t = new_s.get(name, "")
        old_t = old_s.get(name, "")
        if len(new_t) > DIRECTIVE_TEXT_MAX_CHARS:
            raise DirectiveBoundsError(f"text section '{name}': {len(new_t)} chars > {DIRECTIVE_TEXT_MAX_CHARS}")
        if _headers_outside_fences(new_t):
            raise DirectiveBoundsError(f"text section '{name}': contains a markdown header")
        for reason, pattern in DIRECTIVE_TEXT_FORBIDDEN_PATTERNS:
            # a phrase already present in the current section is grandfathered (older seeds say "system prompt")
            old_hits = {h.lower() for h in re.findall(pattern, old_t, re.IGNORECASE)}
            for m in re.finditer(pattern, new_t, re.IGNORECASE):
                if m.group(0).lower() not in old_hits:
                    raise DirectiveBoundsError(f"text section '{name}': forbidden {reason} ({m.group(0)!r})")
        if old_t and new_t != old_t and len(new_t) > DIRECTIVE_TEXT_RATIO_MIN_LEN:
            ratio = difflib.SequenceMatcher(None, old_t, new_t, autojunk=False).ratio()
            if ratio < DIRECTIVE_TEXT_MIN_SIMILARITY:
                raise DirectiveBoundsError(
                    f"text section '{name}': rewrite too large (similarity {ratio:.2f} < {DIRECTIVE_TEXT_MIN_SIMILARITY})"
                )


def directives_equivalent(old_markdown: str, new_markdown: str) -> bool:
    """Same knobs and same text sections (ignores frontmatter and layout)."""
    if changed_knobs(old_markdown, new_markdown):
        return False
    a, b = directive_sections(old_markdown), directive_sections(new_markdown)
    return all(a.get(n, "") == b.get(n, "") for n in DIRECTIVE_TEXT_SECTIONS)


def render_directives(
    current_markdown: str,
    *,
    filters: dict[str, Any] | None,
    research_protocol: str | None = None,
    risk_constraints: str | None = None,
    output_requirements: str | None = None,
) -> str:
    """Render ``active_directives.md`` from the structured Overseer output.

    Knobs omitted from ``filters`` keep their current value; omitted (null) text sections keep the
    current text. Unknown filter keys are kept so the bounds check rejects them.
    """
    merged: dict[str, Any] = dict(effective_knobs(current_markdown))
    merged.update(filters or {})
    cur = directive_sections(current_markdown)
    texts = {
        "Research Protocol": research_protocol,
        "Risk Constraints": risk_constraints,
        "Output Requirements": output_requirements,
    }
    parts = ["# Active Directives"]
    for name in ("Research Protocol", "Filter Weightings", "Risk Constraints", "Output Requirements"):
        if name == "Filter Weightings":
            block = yaml.safe_dump({"filters": merged}, sort_keys=False, default_flow_style=False).rstrip()
            parts.append(f"## Filter Weightings\n\n```yaml\n{block}\n```")
            continue
        text = texts[name]
        text = cur.get(name, "") if text is None else str(text).strip()
        parts.append(f"## {name}\n\n{text}")
    head = f"---\ntitle: Active Directives\nlast_updated: {datetime.now(timezone.utc).date().isoformat()}\n"
    pipeline = _frontmatter(current_markdown).get("research_pipeline")
    if pipeline:
        head += f"research_pipeline: {pipeline}\n"
    return head + "---\n\n" + "\n\n".join(parts) + "\n"


def normalize_overseer_output(parsed: dict[str, Any], current_markdown: str) -> dict[str, Any]:
    """Return ``parsed`` with ``new_directives_markdown`` set (rendered from the structured fields).

    A legacy reply that already carries ``new_directives_markdown`` is passed through untouched.
    Without either, ``parsed`` is returned as is and payload validation rejects it.
    """
    if not isinstance(parsed, dict):
        return parsed
    if isinstance(parsed.get("new_directives_markdown"), str) and parsed["new_directives_markdown"].strip():
        return parsed
    if not isinstance(parsed.get("filters"), dict):
        return parsed
    out = dict(parsed)
    out["new_directives_markdown"] = render_directives(
        current_markdown,
        filters=parsed["filters"],
        research_protocol=parsed.get("research_protocol"),
        risk_constraints=parsed.get("risk_constraints"),
        output_requirements=parsed.get("output_requirements"),
    )
    return out


__all__ = [
    "DirectiveBoundsError",
    "changed_knobs",
    "directive_sections",
    "directives_equivalent",
    "normalize_overseer_output",
    "render_directives",
    "validate_directive_text",
    "directive_version",
    "effective_knobs",
    "extract_filter_directives",
    "parse_filters_block",
    "validate_directive_change",
]
