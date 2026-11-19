"""Code-side hallucination guard for ``web_searcher`` output.

The agent must emit lines ``- <fact> | <url> | <published YYYY-MM-DD or unknown>``. A line is kept only if it
carries a URL that the provider reported retrieving (``citations``). A web call whose provider reported no
citations at all (``None``) is unverifiable: every line is dropped. A citation entry without a scheme is a
bare domain (Gemini grounding titles; its grounding URIs are opaque redirect links) and matches any URL on that
host. ``NOT_FOUND`` passes through; if every line of a result is dropped the result becomes ``NOT_FOUND``.
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
    dropped_unverifiable: int = 0  # web call without any citations: lines cannot be checked
    not_found: int = 0  # the model answered NOT_FOUND (nothing relevant on the pages); nothing was dropped
    dropped_blank: int = 0  # blank / NOT_FOUND filler lines mixed in with fact lines

    def add(self, other: "FilterCounts") -> None:
        self.kept += other.kept
        self.dropped_no_url += other.dropped_no_url
        self.dropped_unretrieved += other.dropped_unretrieved
        self.dropped_unverifiable += other.dropped_unverifiable
        self.not_found += other.not_found
        self.dropped_blank += other.dropped_blank

    def reason(self) -> str:
        """Compact "why nothing was kept" for logs: every path has a named counter."""
        return (
            f"not_found={self.not_found} dropped_no_url={self.dropped_no_url} "
            f"dropped_unretrieved={self.dropped_unretrieved} dropped_unverifiable={self.dropped_unverifiable} "
            f"dropped_blank={self.dropped_blank}"
        )


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


def _host(url: str) -> str:
    return normalize_url(url).split("/", 1)[0].split("?", 1)[0]


def filter_research_data(
    text: str, citations: list[str] | None, *, strict: bool = True
) -> tuple[str, FilterCounts]:
    """Filter one result's ``research_data``; returns (new text, counts).

    ``citations is None`` with ``strict`` (the default) means unverifiable: URL lines are dropped. Pass
    ``strict=False`` only when no provider web call happened (stub / injected runner).
    """
    counts = FilterCounts()
    stripped = str(text or "").strip()
    if not stripped:
        counts.dropped_blank += 1
        return stripped, counts
    if stripped == NOT_FOUND:
        counts.not_found += 1
        return stripped, counts
    cites = citations or []
    allowed = {normalize_url(c) for c in cites if "//" in c}
    domains = {normalize_url(c) for c in cites if "//" not in c and "." in c and " " not in c}
    kept: list[str] = []
    for line in stripped.splitlines():
        if not line.strip() or line.strip() == NOT_FOUND:
            counts.dropped_blank += 1
            continue
        url = _line_url(line)
        if url is None:
            counts.dropped_no_url += 1
        elif citations is None and strict:
            counts.dropped_unverifiable += 1
        elif citations is not None and normalize_url(url) not in allowed and not any(
            _host(url) == d or _host(url).endswith("." + d) for d in domains
        ):
            counts.dropped_unretrieved += 1
        else:
            kept.append(line)
            counts.kept += 1
    return ("\n".join(kept) if kept else NOT_FOUND), counts


def filter_rows(
    rows: list[dict[str, Any]], citations: list[str] | None, *, strict: bool = True
) -> tuple[list[dict[str, Any]], FilterCounts]:
    """Apply :func:`filter_research_data` to every non-error row."""
    total = FilterCounts()
    out = []
    for r in rows:
        if r.get("error") or not str(r.get("research_data") or "").strip():
            out.append(r)
            continue
        data, c = filter_research_data(r["research_data"], citations, strict=strict)
        total.add(c)
        out.append({**r, "research_data": data})
    return out, total
