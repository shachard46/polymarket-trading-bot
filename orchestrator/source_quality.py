"""Source-quality tag for an evidence URL: official | major | aggregator | blog | unknown.

The domain map lives in ``config/source_quality.yaml`` (editable); an unlisted domain falls back to a small
heuristic (government/education TLDs are official, forum/blog hosts are blogs), else ``unknown``. An unknown tier
name or malformed file raises.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from config.trading_constants import EVIDENCE_QUALITY_TIERS

UNKNOWN = "unknown"
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config" / "source_quality.yaml"

_OFFICIAL_SUFFIXES = (".gov", ".mil", ".int", ".edu", ".gov.uk", ".gov.au", ".gc.ca")
_BLOG_HINTS = ("blog", "forum", "wordpress", "substack", "medium.com", "reddit", "wixsite", "tumblr")
_AGGREGATOR_HINTS = ("wiki",)


def host_of(url: str) -> str:
    host = (urlsplit(str(url or "").strip()).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


@lru_cache(maxsize=4)
def load_map(path: str = str(DEFAULT_PATH)) -> dict[str, str]:
    """domain -> tier from the YAML file; raises on an unknown tier or a malformed file."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping of tier -> domains")
    out: dict[str, str] = {}
    for tier, domains in data.items():
        if tier not in EVIDENCE_QUALITY_TIERS or tier == UNKNOWN:
            raise ValueError(f"{path}: unknown source-quality tier {tier!r}")
        if not isinstance(domains, list):
            raise ValueError(f"{path}: tier {tier!r} must list domains")
        for d in domains:
            out[str(d).strip().lower()] = tier
    return out


def quality_for(url: str, path: str | Path | None = None) -> str:
    """Tier of ``url``'s domain (exact or subdomain match in the map, else heuristic, else ``unknown``)."""
    host = host_of(url)
    if not host:
        return UNKNOWN
    mapping = load_map(str(path or DEFAULT_PATH))
    parts = host.split(".")
    for i in range(len(parts) - 1):
        tier = mapping.get(".".join(parts[i:]))
        if tier:
            return tier
    if host.endswith(_OFFICIAL_SUFFIXES):
        return "official"
    if any(h in host for h in _BLOG_HINTS):
        return "blog"
    if any(h in host for h in _AGGREGATOR_HINTS):
        return "aggregator"
    return UNKNOWN


__all__ = ["host_of", "load_map", "quality_for"]
