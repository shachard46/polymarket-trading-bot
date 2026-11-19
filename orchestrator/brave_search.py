"""Brave Web Search + page fetch: the raw material for the ``web_extractor`` agent.

For one query: Brave Web Search (up to ``BRAVE_RESULTS_PER_QUERY`` results), then the top
``BRAVE_FETCH_PAGES`` pages are downloaded (size and time capped) and reduced to plain text with the stdlib.
Everything returned in a :class:`Page` is what the extractor sees, so the URLs of the pages are exactly the
citations the code-side guard (``web_filter``) accepts. Network errors on the search raise :class:`BraveError`;
a page that cannot be fetched falls back to its Brave snippets. Price-blindness is enforced later, by sanitizing the
extractor's output.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import requests

from config.trading_constants import (
    BRAVE_BACKOFF_CAP_SEC,
    BRAVE_FETCH_PAGES,
    BRAVE_MAX_RETRIES,
    BRAVE_PAGE_MAX_BYTES,
    BRAVE_PAGE_TEXT_CHARS,
    BRAVE_PAGE_TIMEOUT_SEC,
    BRAVE_RESULTS_PER_QUERY,
    BRAVE_SEARCH_TIMEOUT_SEC,
    BRAVE_USER_AGENT,
)
from orchestrator.util import truncate_lines

log = logging.getLogger(__name__)

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
API_KEY_ENV = "BRAVE_API_KEY"

_sleep = time.sleep  # patched in tests

# Process-wide default for search(..., freshness=None); set by the backtest harness (worker threads
# do not inherit contextvars) so every Brave call in a research run is date-limited.
_FRESHNESS_OVERRIDE: str | None = None


@contextmanager
def freshness_override(freshness: str | None):
    """Within the block, searches without an explicit freshness use this value (pd/pw/pm/py or a date range)."""
    global _FRESHNESS_OVERRIDE
    prev, _FRESHNESS_OVERRIDE = _FRESHNESS_OVERRIDE, freshness
    try:
        yield
    finally:
        _FRESHNESS_OVERRIDE = prev


class BraveError(Exception):
    """The Brave search request failed (no key, HTTP error, bad body, rate limit not cleared)."""


@dataclass
class Page:
    url: str
    title: str
    page_age: str | None  # YYYY-MM-DD from Brave's page_age, else None
    text: str  # snippets + fetched page text, truncated
    fetched: bool  # True when the page body itself was read (not only snippets)


def _headers() -> dict[str, str]:
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise BraveError(f"{API_KEY_ENV} is not set")
    return {"Accept": "application/json", "X-Subscription-Token": key, "User-Agent": BRAVE_USER_AGENT}


def _retry_wait(resp: Any, attempt: int) -> float:
    for name in ("Retry-After", "X-RateLimit-Reset"):
        try:
            return min(max(float(resp.headers.get(name)), 0.0), BRAVE_BACKOFF_CAP_SEC)
        except (TypeError, ValueError):
            continue
    return min(2.0 ** attempt, BRAVE_BACKOFF_CAP_SEC)


def search(query: str, freshness: str | None = None) -> list[dict[str, Any]]:
    """Brave Web Search results (web.results) for query; backs off and retries on HTTP 429.

    freshness (optional): pd/pw/pm/py or a date range "YYYY-MM-DDtoYYYY-MM-DD".
    """
    headers = _headers()
    params = {"q": query, "count": BRAVE_RESULTS_PER_QUERY, "extra_snippets": "true"}
    freshness = freshness or _FRESHNESS_OVERRIDE
    if freshness:
        params["freshness"] = freshness
    for attempt in range(BRAVE_MAX_RETRIES + 1):
        try:
            resp = requests.get(BRAVE_URL, headers=headers, params=params, timeout=BRAVE_SEARCH_TIMEOUT_SEC)
        except requests.RequestException as exc:
            raise BraveError(f"request failed: {exc}") from exc
        if resp.status_code == 429:
            if attempt >= BRAVE_MAX_RETRIES:
                raise BraveError("rate limited (HTTP 429) after retries")
            wait = _retry_wait(resp, attempt)
            log.warning("[BRAVE] 429; waiting %.1fs (attempt %d)", wait, attempt + 1)
            _sleep(wait)
            continue
        if resp.status_code != 200:
            raise BraveError(f"HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise BraveError(f"bad JSON: {exc}") from exc
        results = ((body or {}).get("web") or {}).get("results") or []
        return [r for r in results if isinstance(r, dict) and r.get("url")]
    raise BraveError("unreachable")  # pragma: no cover


# ---- HTML -> text ---------------------------------------------------------------------------------

_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "head", "nav", "footer", "form", "iframe"})
_BLOCK_TAGS = frozenset({
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "header", "main", "aside", "dd", "dt",
})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Visible text of an HTML document: scripts/styles/navigation dropped, whitespace collapsed per line."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup: keep what was parsed
        pass
    lines = (re.sub(r"\s+", " ", ln).strip() for ln in "".join(parser.parts).splitlines())
    return "\n".join(ln for ln in lines if ln)


def _safe_url(url: str) -> bool:
    """http(s) only, and never a loopback / private / link-local literal or local-looking host."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return False
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def fetch_page_text(url: str) -> str | None:
    """Plain text of an HTML page, or None (unsafe URL, error, non-HTML such as PDF, empty)."""
    if not _safe_url(url):
        return None
    try:
        resp = requests.get(
            url, headers={"User-Agent": BRAVE_USER_AGENT, "Accept": "text/html,text/plain;q=0.8"},
            timeout=BRAVE_PAGE_TIMEOUT_SEC, stream=True,
        )
    except requests.RequestException:
        return None
    try:
        if resp.status_code != 200:
            return None
        ctype = str(resp.headers.get("Content-Type", "")).lower()
        if ctype and "html" not in ctype and "text/plain" not in ctype:
            return None
        chunks: list[bytes] = []
        size = 0
        for chunk in resp.iter_content(chunk_size=16384):
            chunks.append(chunk)
            size += len(chunk)
            if size >= BRAVE_PAGE_MAX_BYTES:
                break
        raw = b"".join(chunks)[:BRAVE_PAGE_MAX_BYTES]
        html = raw.decode(resp.encoding or "utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None
    finally:
        resp.close()
    text = html_to_text(html) if "text/plain" not in ctype else html
    return text.strip() or None


def _page_age_date(raw: Any) -> str | None:
    m = re.match(r"\s*(\d{4}-\d{2}-\d{2})", str(raw or ""))
    return m.group(1) if m else None


def _snippets(result: dict[str, Any]) -> str:
    parts = [str(result.get("description") or "")] + [str(s) for s in result.get("extra_snippets") or []]
    return "\n".join(p.strip() for p in parts if p and p.strip())


def _build_page(result: dict[str, Any]) -> Page | None:
    url = str(result["url"])
    body = fetch_page_text(url)
    snippets = _snippets(result)
    text = "\n".join(p for p in (snippets, body) if p)
    if not text.strip():
        return None
    return Page(
        url=url,
        title=str(result.get("title") or ""),
        page_age=_page_age_date(result.get("page_age")),
        text=truncate_lines(text, BRAVE_PAGE_TEXT_CHARS),
        fetched=body is not None,
    )


def search_pages(query: str, fetch_pages: int = BRAVE_FETCH_PAGES, freshness: str | None = None) -> list[Page]:
    """Search ``query`` and read the top ``fetch_pages`` results; raises :class:`BraveError` on a failed search."""
    results = search(query, freshness=freshness)[: max(0, fetch_pages)]
    if not results:
        return []
    with ThreadPoolExecutor(max_workers=len(results)) as pool:
        pages = list(pool.map(_build_page, results))
    return [p for p in pages if p is not None]


__all__ = ["BraveError", "Page", "fetch_page_text", "freshness_override", "html_to_text", "search", "search_pages"]
