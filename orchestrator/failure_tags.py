"""Open failure tags: a vault registry of post-mortem tags beyond the fixed failure classes.

The Post-Mortem Analyst returns a fixed class or `new:<slug>` plus a one-line definition. Code owns the
registry (`00_System/failure_tags.json`): {slug: {definition, count, first_seen, last_seen}}. A tag with
`count >= FAILURE_TAG_MIN_COUNT` is *established* (counts as evidence for the Overseer); below that it is a
*candidate* (shown to the Overseer, clearly not evidence). Invalid class/slug -> `unclassified`.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from typing import Any

from config.trading_constants import FAILURE_TAG_MIN_COUNT
from obsidian_utils import FAILURE_CLASSES, ObsidianManager, _atomic_write_text

UNCLASSIFIED = "unclassified"
TAG_PREFIX = "new:"
SLUG_RE = re.compile(r"^[a-z0-9_]{3,40}$")
REGISTRY_FILENAME = "failure_tags.json"
MAX_DEFINITION_CHARS = 200
_lock = threading.Lock()


def _path(vault: ObsidianManager):
    return vault._dirs["system"] / REGISTRY_FILENAME


def load_registry(vault: ObsidianManager) -> dict[str, dict[str, Any]]:
    """Registry as {slug: {definition, count, first_seen, last_seen}}; {} when missing/corrupt."""
    try:
        data = json.loads(_path(vault).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in data.items()
        if isinstance(k, str) and SLUG_RE.match(k) and isinstance(v, dict)
    }


def _count(entry: dict[str, Any]) -> int:
    try:
        return int(entry.get("count") or 0)
    except (TypeError, ValueError):
        return 0


def established(registry: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {s: e for s, e in registry.items() if _count(e) >= FAILURE_TAG_MIN_COUNT}


def candidates(registry: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {s: e for s, e in registry.items() if _count(e) < FAILURE_TAG_MIN_COUNT}


def normalize_failure_class(
    raw: Any, definition: Any, registry: dict[str, dict[str, Any]]
) -> tuple[str, str | None, str | None]:
    """Return ``(failure_class, slug, definition)`` for the ledger.

    Fixed class -> (class, None, None). Valid tag -> (`new:slug`, slug, one-line definition). A tag
    needs a definition unless it is already in the registry (reuse). Anything else -> unclassified.
    """
    if not isinstance(raw, str):
        return UNCLASSIFIED, None, None
    cleaned = raw.strip().lower()
    if cleaned in FAILURE_CLASSES:
        return cleaned, None, None
    slug = cleaned[len(TAG_PREFIX):] if cleaned.startswith(TAG_PREFIX) else cleaned
    if not SLUG_RE.match(slug):
        return UNCLASSIFIED, None, None
    if not cleaned.startswith(TAG_PREFIX) and slug not in registry:
        return UNCLASSIFIED, None, None  # a bare slug is only accepted as reuse of a known tag
    text = " ".join(definition.split())[:MAX_DEFINITION_CHARS] if isinstance(definition, str) else ""
    if not text and slug not in registry:
        return UNCLASSIFIED, None, None
    return TAG_PREFIX + slug, slug, text or None


def record_tag(vault: ObsidianManager, slug: str, definition: str | None) -> dict[str, Any]:
    """Count one resolved post-mortem against `slug` (first definition wins). Atomic write."""
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        reg = load_registry(vault)
        entry = reg.get(slug)
        if entry is None:
            entry = {"definition": definition or "", "count": 0, "first_seen": now, "last_seen": now}
        entry["count"] = _count(entry) + 1
        entry["last_seen"] = now
        if not entry.get("definition") and definition:
            entry["definition"] = definition
        reg[slug] = entry
        _atomic_write_text(_path(vault), json.dumps(reg, indent=2, sort_keys=True))
        return entry


def post_mortem_tag_context(registry: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Payload block for the analyst: fixed list + established + candidate tags with definitions."""
    def view(d: dict[str, dict[str, Any]]) -> dict[str, str]:
        return {TAG_PREFIX + s: str(e.get("definition") or "") for s, e in sorted(d.items())}

    return {
        "failure_classes": sorted(FAILURE_CLASSES),
        "established_failure_tags": view(established(registry)),
        "candidate_failure_tags": view(candidates(registry)),
    }


def overseer_failure_views(
    counts: dict[str, int], registry: dict[str, dict[str, Any]]
) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    """Split ledger class counts: (counts incl. established tags, candidate tags with count+definition)."""
    est = established(registry)
    merged: dict[str, int] = {}
    for key, n in counts.items():
        if key.startswith(TAG_PREFIX):
            slug = key[len(TAG_PREFIX):]
            if slug not in est:
                continue
        merged[key] = n
    cand = {
        TAG_PREFIX + s: {"count": _count(e), "definition": str(e.get("definition") or "")}
        for s, e in sorted(candidates(registry).items())
    }
    return merged, cand
