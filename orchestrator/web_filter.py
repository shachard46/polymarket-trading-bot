"""Code-side hallucination guard for ``web_searcher`` output.

The agent must emit lines ``- <fact> | <url> | <published YYYY-MM-DD or unknown>``. A line is kept only if it
carries a URL and, when the provider reported which URLs it retrieved (``citations``), that URL is among them.
``NOT_FOUND`` passes through; if every line of a result is dropped the result becomes ``NOT_FOUND``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

NOT_FOUND = "NOT_FOUND"
_URL_RE = re.compile(r"https?://[^\s|<>\]\[`\"']+", re.IGNORECASE)


@dataclass
class FilterCounts:
    kept: int = 0
    dropped_no_url: int = 0
    dropped_unretrieved: int = 0

    def add(self, other: "FilterCounts") -> None:
        self.kept += other.kept
        self.dropped_no_url += other.dropped_no_url
        self.dropped_unretrieved += other.dropped_unretrieved


def normalize_url(url: str) -> str:
    """Comparable form: lowercase scheme-less host without ``www.``, no fragment, no trailing slash."""
    u = url.strip().rstrip(".,;:)")
    parts = urlsplit(u if "//" in u else "//" + u)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    return host + path + (f"?{parts.query}" if parts.query else "")


def _line_url(line: str) -> str | None:
    m = _URL_RE.search(line)
    return m.group(0).rstrip(".,;:)") if m else None


def filter_research_data(text: str, citations: list[str] | None) -> tuple[str, FilterCounts]:
    """Filter one result's ``research_data``; returns (new text, counts)."""
    counts = FilterCounts()
    stripped = str(text or "").strip()
    if not stripped or stripped == NOT_FOUND:
        return stripped, counts
    allowed = None if citations is None else {normalize_url(c) for c in citations}
    kept: list[str] = []
    for line in stripped.splitlines():
        if not line.strip():
            continue
        url = _line_url(line)
        if url is None:
            counts.dropped_no_url += 1
        elif allowed is not None and normalize_url(url) not in allowed:
            counts.dropped_unretrieved += 1
        else:
            kept.append(line)
            counts.kept += 1
    return ("\n".join(kept) if kept else NOT_FOUND), counts


def filter_rows(
    rows: list[dict[str, Any]], citations: list[str] | None
) -> tuple[list[dict[str, Any]], FilterCounts]:
    """Apply :func:`filter_research_data` to every non-error row."""
    total = FilterCounts()
    out = []
    for r in rows:
        if r.get("error") or not str(r.get("research_data") or "").strip():
            out.append(r)
            continue
        data, c = filter_research_data(r["research_data"], citations)
        total.add(c)
        out.append({**r, "research_data": data})
    return out, total
