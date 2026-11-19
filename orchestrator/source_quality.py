"""Source-quality tag for an evidence URL: official | major | aggregator | blog | unknown.

Lookup order for a domain: (1) ``config/source_quality.yaml`` (editable, user edits always win), (2) the persisted
classifier cache ``Vault/00_System/source_quality_cache.json`` (domain -> {tier, ts, by}, filled by the cheap
``source_classifier`` role), (3) deterministic heuristics (government/military/treaty TLDs, official statistics
offices, central banks, courts = official; the big wires = major; forum/blog hosts = blog), (4) ``unknown``.
Domains that none of 1-3 resolve are what the pipeline sends, in one batched call per research, to the classifier
(see :func:`unresolved_domains` / :func:`store`); a cached domain is never asked again. An unknown tier name or a
malformed YAML file raises; a malformed cache file is ignored.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from config.trading_constants import EVIDENCE_QUALITY_TIERS

UNKNOWN = "unknown"
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config" / "source_quality.yaml"
CACHE_FILENAME = "source_quality_cache.json"
CLASSIFIER_ROLE = "source_classifier"

_OFFICIAL_SUFFIXES = (".gov", ".mil", ".int", ".edu", ".gov.uk", ".gov.au", ".gc.ca")
# whole host labels that mark an official statistics office, central bank or court
_OFFICIAL_LABELS = frozenset({
    "centralbank", "bundesbank", "federalreserve", "bankofengland", "bankofcanada", "ecb", "census", "destatis",
    "insee", "istat", "eurostat", "bls", "statcan", "statistics", "supremecourt", "uscourts", "courts",
})
_MAJOR_WIRES = ("reuters.com", "apnews.com", "afp.com", "bloomberg.com")
_BLOG_HINTS = ("blog", "forum", "wordpress", "substack", "medium.com", "reddit", "wixsite", "tumblr")
_AGGREGATOR_HINTS = ("wiki",)
_SECOND_LEVEL = frozenset({"co", "com", "org", "gov", "ac", "net", "edu"})

_lock = threading.Lock()
_cache_path: Path | None = None
_cache_mem: tuple[Path, int, dict[str, str]] | None = None


def host_of(url: str) -> str:
    host = (urlsplit(str(url or "").strip()).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def domain_key(host: str) -> str:
    """Registrable-ish domain of a host (``news.bbc.co.uk`` -> ``bbc.co.uk``); the cache key."""
    parts = [p for p in host.lower().split(".") if p]
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


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


# ---- persisted classifier cache -----------------------------------------------------------------------------


def bind_cache(path: str | Path | None) -> None:
    """Use ``path`` as the classifier cache (``None``: no cache is read). Called by the pipeline with the vault's file."""
    global _cache_path
    with _lock:
        _cache_path = Path(path) if path is not None else None


def cache_path_for(vault: object) -> Path:
    return vault._dirs["system"] / CACHE_FILENAME  # type: ignore[attr-defined]


def _read_cache() -> dict[str, str]:
    """domain -> tier of the bound cache file (re-read when its mtime changes; bad file or entries ignored)."""
    global _cache_mem
    path = _cache_path
    if path is None:
        return {}
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {}
    if _cache_mem is not None and _cache_mem[0] == path and _cache_mem[1] == mtime:
        return _cache_mem[2]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for dom, entry in raw.items():
            tier = entry.get("tier") if isinstance(entry, dict) else None
            if tier in EVIDENCE_QUALITY_TIERS:
                out[str(dom).strip().lower()] = tier
    _cache_mem = (path, mtime, out)
    return out


def store(tiers: dict[str, str], by: str = CLASSIFIER_ROLE) -> int:
    """Persist ``domain -> tier`` (atomic write, merged with what is there); returns the number stored."""
    from obsidian_utils.schemas import _atomic_write_text  # noqa: PLC0415

    path = _cache_path
    if path is None or not tiers:
        return 0
    with _lock:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        now = datetime.now(tz=timezone.utc).isoformat()
        n = 0
        for dom, tier in tiers.items():
            if tier in EVIDENCE_QUALITY_TIERS:
                raw[domain_key(dom)] = {"tier": tier, "ts": now, "by": by}
                n += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, json.dumps(raw, indent=2, sort_keys=True))
    return n


# ---- lookup ---------------------------------------------------------------------------------------------------


def _suffix_lookup(mapping: dict[str, str], host: str) -> str | None:
    parts = host.split(".")
    for i in range(len(parts) - 1):
        tier = mapping.get(".".join(parts[i:]))
        if tier:
            return tier
    return None


def heuristic_tier(host: str) -> str:
    """Deterministic guess from the host alone (``unknown`` when nothing matches)."""
    labels = host.split(".")
    if (
        host.endswith(_OFFICIAL_SUFFIXES)
        or any(label in _OFFICIAL_LABELS for label in labels)
        or any(label in ("gov", "gob", "govt") for label in labels[1:])  # gov.cn, gob.mx, govt.nz ...
    ):
        return "official"
    if any(host == w or host.endswith("." + w) for w in _MAJOR_WIRES):
        return "major"
    if any(h in host for h in _BLOG_HINTS):
        return "blog"
    if any(h in host for h in _AGGREGATOR_HINTS):
        return "aggregator"
    return UNKNOWN


def quality_for(url: str, path: str | Path | None = None) -> str:
    """Tier of ``url``'s domain: YAML map, then classifier cache, then heuristic, else ``unknown``."""
    host = host_of(url)
    if not host:
        return UNKNOWN
    return (
        _suffix_lookup(load_map(str(path or DEFAULT_PATH)), host)
        or _suffix_lookup(_read_cache(), host)
        or heuristic_tier(host)
    )


def unresolved_domains(urls: list[str], path: str | Path | None = None) -> list[str]:
    """Distinct domains (cache keys, first-seen order) that the YAML map, the cache and the heuristics all leave open."""
    mapping = load_map(str(path or DEFAULT_PATH))
    cache = _read_cache()
    out: list[str] = []
    for url in urls:
        host = host_of(url)
        if not host or "." not in host:
            continue
        if _suffix_lookup(mapping, host) or _suffix_lookup(cache, host) or heuristic_tier(host) != UNKNOWN:
            continue
        key = domain_key(host)
        if key not in out:
            out.append(key)
    return out


__all__ = [
    "bind_cache", "cache_path_for", "domain_key", "heuristic_tier", "host_of", "load_map", "quality_for", "store",
    "unresolved_domains",
]
