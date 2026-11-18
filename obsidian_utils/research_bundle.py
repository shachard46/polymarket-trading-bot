"""Research-bundle persistence: merge helpers and a mixin for ObsidianManager."""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schemas import _atomic_write_text, _check_market_id

log = logging.getLogger(__name__)


def _has_usable_research_data(entry: dict[str, Any]) -> bool:
    return bool(str(entry.get("research_data", "")).strip())


def _should_replace_bundle_entry(prior: dict[str, Any], new: dict[str, Any]) -> bool:
    """Replace a prior bundle row when a refetch succeeds after failure."""
    if not _has_usable_research_data(new):
        return False
    if prior.get("error") or not _has_usable_research_data(prior):
        return True
    return False


def _merge_research_bundle_queries(
    existing: list[dict[str, Any]],
    new_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Append ``new_entries``; refresh duplicate ``query`` rows when the new fetch is better."""
    merged = list(existing)
    index_by_query: dict[str, int] = {
        str(e.get("query")): i for i, e in enumerate(merged) if e.get("query")
    }
    for entry in new_entries:
        query = entry.get("query")
        if not query:
            merged.append(entry)
            continue
        key = str(query)
        if key in index_by_query:
            idx = index_by_query[key]
            if _should_replace_bundle_entry(merged[idx], entry):
                merged[idx] = entry
            continue
        index_by_query[key] = len(merged)
        merged.append(entry)
    return merged


class ResearchBundleMixin:
    """Hub-persisted A-IQ query results (``research_bundles/``)."""

    _dirs: dict[str, Path]

    @property
    def _research_bundles_dir(self) -> Path:
        """Directory for Hub-persisted A-IQ query results (``research_bundles/``)."""
        directory = self._dirs["active"] / "research_bundles"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _research_bundle_path(self, market_id: str) -> Path:
        _check_market_id(market_id)
        return self._research_bundles_dir / f"{market_id}.json"

    def read_research_bundle(self, market_id: str) -> list[dict[str, Any]] | None:
        """Return the ``queries`` list from a saved bundle, or ``None`` if missing.

        An on-disk envelope with an empty ``queries`` array returns ``[]`` (no
        salvaged data — Phase 3 will run the Query Planner).
        """
        path = self._research_bundle_path(market_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        queries = raw.get("queries")
        if not isinstance(queries, list):
            return None
        if not queries:
            return []
        return [q for q in queries if isinstance(q, dict)]

    def read_research_bundle_meta(self, market_id: str) -> dict[str, Any]:
        """``{evidence_source, evidence_fallback}`` stored with the bundle (empty if absent)."""
        path = self._research_bundle_path(market_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {k: raw[k] for k in ("evidence_source", "evidence_fallback") if k in raw}

    def write_research_bundle(
        self,
        market_id: str,
        bundle: list[dict[str, Any]],
        meta: dict[str, Any] | None = None,
    ) -> Path:
        """Write or merge A-IQ results under ``research_bundles/{market_id}.json``.

        New entries are appended; duplicate ``query`` strings are skipped.
        """
        path = self._research_bundle_path(market_id)
        existing: list[dict[str, Any]] = []
        if path.exists():
            prior = self.read_research_bundle(market_id)
            if prior:
                existing = prior
            elif prior is None:
                corrupt = path.with_name(path.name + ".corrupt")
                log.error(
                    "Research bundle %s is unreadable or malformed; backing up to %s",
                    path,
                    corrupt,
                )
                shutil.copy2(path, corrupt)

        merged = _merge_research_bundle_queries(existing, bundle)
        envelope = {
            "market_id": market_id,
            "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
            "queries": merged,
        }
        stored = self.read_research_bundle_meta(market_id) if path.exists() else {}
        envelope.update({**stored, **(meta or {})})
        _atomic_write_text(path, json.dumps(envelope, indent=2))
        return path

    def clear_research_bundle(self, market_id: str) -> None:
        """Delete the saved bundle so the next write starts from fresh data."""
        self._research_bundle_path(market_id).unlink(missing_ok=True)
