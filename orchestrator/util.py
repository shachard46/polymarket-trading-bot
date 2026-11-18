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
