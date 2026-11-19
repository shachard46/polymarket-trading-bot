"""Small shared helpers: tolerant float coercion, timestamp parsing, safe identifiers."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


def to_float(value: Any) -> float | None:
    """Float of ``value``, or None for bool/None/non-finite/unparseable input."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def truncate_lines(text: str, max_chars: int) -> str:
    """Cap ``text`` at ``max_chars`` on a line boundary, ending with a "[truncated: N more lines]" marker line.

    Text within the limit is returned unchanged. If not even the first line fits, that line is
    sliced and a plain "[truncated]" marker follows.
    """
    if len(text) <= max_chars:
        return text
    lines = text.splitlines()
    kept: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        marker = f"[truncated: {len(lines) - i - 1} more lines]"
        # characters after this line: its newline plus the marker for the lines still to drop
        if used + len(line) + 1 + len(marker) > max_chars and i > 0:
            break
        kept.append(line)
        used += len(line) + 1
    if len(kept) == 1 and len(kept[0]) + 1 + len("[truncated]") > max_chars:
        room = max(max_chars - len("\n[truncated]"), 0)
        return kept[0][:room] + "\n[truncated]"
    return "\n".join(kept + [f"[truncated: {len(lines) - len(kept)} more lines]"])


def parse_ts(value: Any) -> datetime | None:
    """Aware datetime from a datetime or ISO string (``Z`` ok); naive means UTC; blank/invalid -> None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def safe_key(value: Any) -> str:
    """``value`` as text with every char outside [A-Za-z0-9._-] replaced by '-'."""
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in str(value))
