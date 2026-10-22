"""
ObsidianManager — Pydantic-gated file system layer for the Obsidian Vault.

The Orchestrator is the ONLY caller.  Agents never touch the file system
directly; they return raw dicts that are validated here before any write
is committed.  A ValidationError raises VaultWriteError, which the
Orchestrator catches to flag the market inactive in its native vault directory.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ValidationError, field_validator

from config.trading_constants import (
    F_MAX,
    FILTERS,
    PAPER_TRADE_MODE,
    STATUS_INACTIVE,
    STATUS_KEY,
    VAULT_PATHS,
)
from config.vault import resolve_vault_base

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Error contract
# ---------------------------------------------------------------------------

class VaultWriteError(Exception):
    """Raised when an agent payload fails Pydantic validation.

    The Orchestrator must catch this, flag the market inactive in place, and skip the
    current market — never re-raise into the outer pipeline loop.
    """

    def __init__(self, market_id: str, cause: ValidationError) -> None:
        self.market_id = market_id
        self.cause = cause
        super().__init__(
            f"Vault write aborted for market '{market_id}': {cause}"
        )


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _check_market_id(market_id: str) -> str:
    """Reject market ids that are unsafe to use as filename stems."""
    if (
        not isinstance(market_id, str)
        or not _SAFE_ID_RE.match(market_id)
        or ".." in market_id
    ):
        raise ValueError(f"unsafe market_id for use in a filename: {market_id!r}")
    return market_id


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a same-dir temp file, fsync, and ``os.replace``."""
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Pydantic payload models  (one per agent output schema)
# ---------------------------------------------------------------------------

class FilterLogPayload(BaseModel):
    """Evaluator / Re-Evaluator output — written to 01_Filters/."""

    market_id: str
    passed: bool
    trigger: Optional[str] = None
    confidence_multiplier: float
    details: str
    error: Optional[str] = None
    signal_bundle: Optional[dict[str, Any]] = None
    status: Optional[str] = None
    error_log: Optional[dict[str, Any]] = None
    pending_edge_refresh: bool = False


class ResearchFrontmatter(BaseModel):
    """YAML frontmatter block from the Deep Researcher — written to 02_Active_Research/."""

    market_id: str
    estimated_p: float
    error: Optional[str] = None
    edge_research_refresh_count: int = 0
    edge_research_refresh_attempts: int = 0
    status: Optional[str] = None
    error_log: Optional[dict[str, Any]] = None

    @field_validator("estimated_p")
    @classmethod
    def _p_in_unit_interval(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"estimated_p must be in [0, 1], got {v}")
        return v

    @field_validator("edge_research_refresh_count", "edge_research_refresh_attempts")
    @classmethod
    def _edge_refresh_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("edge_research_refresh_count/attempts must be >= 0")
        return v


class TradeLogPayload(BaseModel):
    """Trade Executioner output — written to 03_Trades/."""

    market_id: str
    allocation_usd: float
    score: Optional[float] = None
    below_edge_threshold: Optional[bool] = None
    executed: bool
    transaction_hash: Optional[str] = None
    error: Optional[str] = None
    status: Optional[str] = None
    error_log: Optional[dict[str, Any]] = None
    p: Optional[float] = None
    p_low: Optional[float] = None
    p_high: Optional[float] = None
    q_entry: Optional[float] = None
    side: Optional[Literal["YES", "NO"]] = None
    shares: Optional[float] = None
    price: Optional[float] = None
    directive_version: Optional[int] = None
    event_id: Optional[str] = None


class PostMortemPayload(BaseModel):
    """Post-Mortem Analyst output — appended under ## Post-Mortem in 04_Post_Mortems/."""

    market_id: str
    post_mortem_analysis: str
    error: Optional[str] = None
    status: Optional[str] = None
    error_log: Optional[dict[str, Any]] = None


# Headers the seed directives ship with and the Overseer must keep
# producing — see ``_build_seed_directives`` and docs/01_architecture.md §3.
_REQUIRED_DIRECTIVE_HEADERS: tuple[str, ...] = (
    "## Research Protocol",
    "## Filter Weightings",
    "## Risk Constraints",
    "## Output Requirements",
)


class DirectivesPayload(BaseModel):
    """Overseer output — used to overwrite 00_System/active_directives.md.

    The ``new_directives_markdown`` blob is structurally validated:

    - It must start with a YAML frontmatter block parseable as a mapping.
    - It must contain every header in ``_REQUIRED_DIRECTIVE_HEADERS`` so the
      Deep Researcher prompt template stays stable across learning loops.

    A free-form Overseer reply that drops one of these headers is treated
    as a contract violation, not a successful update — the Hub keeps the
    prior directives in place rather than poisoning every downstream agent.
    """

    new_directives_markdown: str
    rationale: str
    error: Optional[str] = None

    @field_validator("new_directives_markdown")
    @classmethod
    def _validate_structure(cls, value: str) -> str:
        # Local import avoids a top-level cycle with the orchestrator package.
        from orchestrator.research import split_yaml_frontmatter_markdown

        try:
            split_yaml_frontmatter_markdown(value)
        except ValueError as exc:
            raise ValueError(
                f"new_directives_markdown is missing or has invalid YAML frontmatter: {exc}"
            ) from exc

        missing = [h for h in _REQUIRED_DIRECTIVE_HEADERS if h not in value]
        if missing:
            raise ValueError(
                f"new_directives_markdown is missing required headers: {missing!r}"
            )
        from orchestrator.directives import parse_filters_block

        if parse_filters_block(value) is None:
            raise ValueError(
                "new_directives_markdown '## Filter Weightings' section must contain "
                "a parseable ```yaml block with a 'filters:' mapping"
            )
        return value


# ---------------------------------------------------------------------------
# ObsidianManager
# ---------------------------------------------------------------------------

_DIRECTIVES_FILENAME = "active_directives.md"


def _archive_timestamp() -> str:
    """Return a filesystem-safe UTC timestamp suffix for archived trade filenames.

    ISO 8601 with ``:`` would be illegal on Windows, so we collapse the
    time portion to a continuous digit string with microsecond resolution.
    """
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _dump_frontmatter(data: dict, body: str = "") -> str:
    """Serialise ``data`` as YAML frontmatter followed by ``body``.

    Produces the canonical Obsidian / Jekyll format::

        ---
        key: value
        ---

        <body>
    """
    normalised = json.loads(json.dumps(data, default=str))
    fm = yaml.safe_dump(
        normalised, default_flow_style=False, allow_unicode=True, sort_keys=True
    ).rstrip()
    if body:
        return f"---\n{fm}\n---\n\n{body}"
    return f"---\n{fm}\n---\n"


# Extension used per directory when the archive format is not JSON
_DIR_EXTENSIONS: dict[str, str] = {
    "system": ".md",
    "filters": ".md",
    "active": ".md",
    "trades": ".json",
    "post_mortem": ".md",
}

_FRONTMATTER_DIR_KEYS: frozenset[str] = frozenset({"filters", "active", "post_mortem"})


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


class ObsidianManager:
    """Single point of truth for all Vault I/O.

    Parameters
    ----------
    vault_base:
        Workspace root on disk.  All ``VAULT_PATHS`` entries (e.g.
        ``Vault/00_System/``) are resolved relative to this.  When omitted,
        uses :envvar:`OPENCLAW_VAULT_PATH` if set, otherwise the project root.
    """

    def __init__(self, vault_base: str | Path | None = None) -> None:
        self._base = resolve_vault_base(vault_base)
        self._dirs: dict[str, Path] = {
            key: self._base / Path(rel_path)
            for key, rel_path in VAULT_PATHS.items()
        }
        self._dirs.setdefault("ledger", self._base / "Vault/05_Ledger/")
        self._ensure_dirs()

    # ------------------------------------------------------------------
    # Directory bootstrap
    # ------------------------------------------------------------------

    def _ensure_dirs(self) -> None:
        """Create all vault subdirectories if they do not already exist."""
        for path in self._dirs.values():
            path.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Generic I/O
    # ------------------------------------------------------------------

    def read_file(self, relative_path: str) -> str:
        """Read any file inside the Vault by path relative to ``vault_base``.

        Parameters
        ----------
        relative_path:
            Path relative to the vault root (e.g. ``"00_System/active_directives.md"``).

        Returns
        -------
        str
            Raw file contents.

        Raises
        ------
        FileNotFoundError
            If the file does not exist.
        """
        target = self._base / relative_path
        return target.read_text(encoding="utf-8")

    def write_file(self, relative_path: str, content: str) -> Path:
        """Write raw content to any path inside the Vault.

        Parent directories are created automatically.  Intended for the
        Overseer's directive overwrite and internal helpers; prefer the
        typed write methods for agent payloads.

        Parameters
        ----------
        relative_path:
            Destination relative to ``vault_base``.
        content:
            UTF-8 string to write (overwrites any existing file).

        Returns
        -------
        Path
            Absolute path of the written file.
        """
        target = self._base / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(target, content)
        return target

    # ------------------------------------------------------------------
    # Validated agent-specific writes
    # ------------------------------------------------------------------

    def write_filter_log(self, market_id: str, payload: dict) -> Path:
        """Validate an Evaluator payload and write it to ``01_Filters/``.

        The file is written as a Markdown document whose YAML frontmatter
        contains the entire validated payload as a JSON-compatible block,
        matching the spec: *pure JSON (or .md with only YAML frontmatter)*.

        Parameters
        ----------
        market_id:
            Polymarket condition_id; used as the filename stem.
        payload:
            Raw dict returned by the Evaluator agent.

        Returns
        -------
        Path
            Absolute path of the written file.

        Raises
        ------
        VaultWriteError
            If ``payload`` fails ``FilterLogPayload`` validation.
        """
        _check_market_id(market_id)
        try:
            validated = FilterLogPayload.model_validate(payload)
        except ValidationError as exc:
            raise VaultWriteError(market_id, exc) from exc

        dest = self._dirs["filters"] / f"{market_id}.md"
        _atomic_write_text(dest, _dump_frontmatter(validated.model_dump()))
        return dest

    def is_market_inactive(self, market_id: str, *, dir_key: str = "filters") -> bool:
        """True when the market artifact exists and frontmatter has ``status: inactive``."""
        record = self.read_market_record(market_id, dir_key)
        if not record:
            return False
        return str(record.get(STATUS_KEY) or "").strip().lower() == STATUS_INACTIVE

    def read_filter_log(self, market_id: str) -> dict[str, Any] | None:
        """Return the YAML frontmatter from ``01_Filters/{market_id}.md`` if present.

        Used to give the Re-Evaluator continuity (prior trigger/details) without
        the agent reading the vault directly.
        """
        path = self._dirs["filters"] / f"{market_id}.md"
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        # Local import: generic YAML frontmatter split (filter logs are frontmatter-only).
        from orchestrator.research import split_yaml_frontmatter_markdown

        try:
            fm, _ = split_yaml_frontmatter_markdown(text)
        except ValueError:
            return None
        return fm

    def write_research_report(
        self,
        market_id: str,
        payload: dict,
        body: str,
    ) -> Path:
        """Validate a Deep Researcher payload and write to ``02_Active_Research/``.

        The file format is YAML frontmatter (``market_id``, ``estimated_p``,
        ``error``) followed by the full Markdown body produced by the agent
        (``## Bull Thesis``, ``## Bear Thesis``, ``## Post-Mortem`` sections).

        Parameters
        ----------
        market_id:
            Polymarket condition_id; used as the filename stem.
        payload:
            Dict containing at minimum ``market_id`` and ``estimated_p``.
        body:
            Raw Markdown string (everything after the frontmatter block).

        Returns
        -------
        Path
            Absolute path of the written file.

        Raises
        ------
        VaultWriteError
            If ``payload`` fails ``ResearchFrontmatter`` validation.
        """
        _check_market_id(market_id)
        try:
            validated = ResearchFrontmatter.model_validate(payload)
        except ValidationError as exc:
            raise VaultWriteError(market_id, exc) from exc

        dest = self._dirs["active"] / f"{market_id}.md"
        _atomic_write_text(dest, _dump_frontmatter(validated.model_dump(), body))
        return dest

    def write_trade_log(self, market_id: str, payload: dict) -> Path:
        """Validate a Trade Executioner payload and write to ``03_Trades/``.

        The file is written as a pretty-printed JSON document.

        Parameters
        ----------
        market_id:
            Polymarket condition_id; used as the filename stem.
        payload:
            Raw dict returned by the Trade Executioner agent.

        Returns
        -------
        Path
            Absolute path of the written file.

        Raises
        ------
        VaultWriteError
            If ``payload`` fails ``TradeLogPayload`` validation.
        """
        _check_market_id(market_id)
        try:
            validated = TradeLogPayload.model_validate(payload)
        except ValidationError as exc:
            raise VaultWriteError(market_id, exc) from exc

        dest = self._dirs["trades"] / f"{market_id}.json"
        _atomic_write_text(dest, json.dumps(validated.model_dump(), indent=2))
        return dest

    def append_post_mortem(self, market_id: str, payload: dict) -> Path:
        """Validate a Post-Mortem Analyst payload and append it to the report.

        The target file is expected to exist in ``04_Post_Mortems/`` (moved
        there by the Orchestrator before spawning the analyst).  The analysis
        paragraph is appended directly under the ``## Post-Mortem`` header.

        Parameters
        ----------
        market_id:
            Polymarket condition_id; used as the filename stem.
        payload:
            Raw dict returned by the Post-Mortem Analyst agent.

        Returns
        -------
        Path
            Absolute path of the updated file.

        Raises
        ------
        VaultWriteError
            If ``payload`` fails ``PostMortemPayload`` validation.
        FileNotFoundError
            If no report file exists in ``04_Post_Mortems/`` for this market.
        """
        _check_market_id(market_id)
        try:
            validated = PostMortemPayload.model_validate(payload)
        except ValidationError as exc:
            raise VaultWriteError(market_id, exc) from exc

        dest = self._dirs["post_mortem"] / f"{market_id}.md"
        if not dest.exists():
            raise FileNotFoundError(
                f"Post-mortem report not found: {dest}. "
                "Ensure the Orchestrator moves the file before spawning the analyst."
            )

        content = dest.read_text(encoding="utf-8")
        analysis = validated.post_mortem_analysis.strip()
        header_re = re.compile(r"^## Post-Mortem[ \t]*$", re.MULTILINE)
        match = header_re.search(content)
        if match:
            # Section runs to the next level-2 header (or EOF); replace its content.
            next_h = re.compile(r"^## ", re.MULTILINE).search(content, match.end())
            tail = content[next_h.start():] if next_h else ""
            content = content[: match.end()] + "\n\n" + analysis + "\n"
            if tail:
                content += "\n" + tail
        else:
            content = content.rstrip("\n") + f"\n\n## Post-Mortem\n\n{analysis}\n"

        _atomic_write_text(dest, content)
        return dest

    def write_directives(self, payload: dict) -> Path:
        """Validate an Overseer payload and overwrite ``active_directives.md``.

        The entire file is replaced with ``new_directives_markdown`` from the
        validated payload.  The Orchestrator calls this at the end of each
        macro-learning loop.

        Parameters
        ----------
        payload:
            Raw dict returned by the Overseer agent.

        Returns
        -------
        Path
            Absolute path of the overwritten file.

        Raises
        ------
        VaultWriteError
            If ``payload`` fails ``DirectivesPayload`` validation.
        """
        try:
            validated = DirectivesPayload.model_validate(payload)
        except ValidationError as exc:
            raise VaultWriteError("__system__", exc) from exc

        dest = self._dirs["system"] / _DIRECTIVES_FILENAME
        if dest.exists():
            shutil.copy2(dest, dest.with_name(dest.name + ".bak"))
        _atomic_write_text(dest, validated.new_directives_markdown)
        return dest

    # ------------------------------------------------------------------
    # In-place state patching (frontmatter / JSON)
    # ------------------------------------------------------------------

    def market_file(self, market_id: str, dir_key: str) -> Path | None:
        """Return the on-disk path for ``market_id`` in ``dir_key``, if it exists."""
        if dir_key not in self._dirs:
            raise KeyError(f"Unknown dir_key {dir_key!r}")
        _check_market_id(market_id)
        primary_ext = _DIR_EXTENSIONS.get(dir_key, ".md")
        fallback_ext = ".json" if primary_ext == ".md" else ".md"
        for ext in (primary_ext, fallback_ext):
            path = self._dirs[dir_key] / f"{market_id}{ext}"
            if path.exists():
                return path
        return None

    def read_market_record(self, market_id: str, dir_key: str) -> dict[str, Any] | None:
        """Read parsed frontmatter or JSON root for a market artifact."""
        path = self.market_file(market_id, dir_key)
        if path is None:
            return None
        if dir_key in _FRONTMATTER_DIR_KEYS or path.suffix == ".md":
            from orchestrator.research import split_yaml_frontmatter_markdown

            try:
                fm, _ = split_yaml_frontmatter_markdown(
                    path.read_text(encoding="utf-8")
                )
            except ValueError:
                return None
            return fm
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return raw if isinstance(raw, dict) else None

    def iter_dir_files(self, dir_key: str, suffix: str) -> list[Path]:
        """Return sorted files under a vault directory matching ``suffix``."""
        if dir_key not in self._dirs:
            raise KeyError(f"Unknown dir_key {dir_key!r}")
        return sorted(self._dirs[dir_key].glob(f"*{suffix}"))

    def patch_frontmatter(
        self,
        market_id: str,
        dir_key: str,
        updates: dict[str, Any],
    ) -> Path | None:
        """Merge ``updates`` into YAML frontmatter; preserve markdown body."""
        path = self.market_file(market_id, dir_key)
        if path is None:
            return None
        from orchestrator.research import split_yaml_frontmatter_markdown

        text = path.read_text(encoding="utf-8")
        try:
            fm, body = split_yaml_frontmatter_markdown(text)
        except ValueError:
            return None
        merged = {**fm, **updates}
        _atomic_write_text(path, _dump_frontmatter(merged, body))
        return path

    def patch_json(
        self,
        market_id: str,
        dir_key: str,
        updates: dict[str, Any],
    ) -> Path | None:
        """Merge ``updates`` into a JSON trade log root object."""
        path = self.market_file(market_id, dir_key)
        if path is None:
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        merged = {**raw, **updates}
        _atomic_write_text(path, json.dumps(merged, indent=2))
        return path

    def strip_keys(
        self,
        market_id: str,
        dir_key: str,
        keys: tuple[str, ...],
        *,
        dry_run: bool = False,
    ) -> Path | None:
        """Remove keys from frontmatter or JSON; rewrite file in place."""
        path = self.market_file(market_id, dir_key)
        if path is None:
            return None
        if dry_run:
            return path
        if dir_key in _FRONTMATTER_DIR_KEYS or path.suffix == ".md":
            from orchestrator.research import split_yaml_frontmatter_markdown

            text = path.read_text(encoding="utf-8")
            try:
                fm, body = split_yaml_frontmatter_markdown(text)
            except ValueError:
                return None
            for key in keys:
                fm.pop(key, None)
            _atomic_write_text(path, _dump_frontmatter(fm, body))
            return path
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        for key in keys:
            raw.pop(key, None)
        _atomic_write_text(path, json.dumps(raw, indent=2))
        return path

    # ------------------------------------------------------------------
    # Trade archival (phase 5)
    # ------------------------------------------------------------------

    @property
    def _trades_archive_dir(self) -> Path:
        archive = self._dirs["trades"] / "_resolved"
        archive.mkdir(parents=True, exist_ok=True)
        return archive

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

    def write_research_bundle(
        self,
        market_id: str,
        bundle: list[dict[str, Any]],
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
        _atomic_write_text(path, json.dumps(envelope, indent=2))
        return path

    def clear_research_bundle(self, market_id: str) -> None:
        """Delete the saved bundle so the next write starts from fresh data."""
        self._research_bundle_path(market_id).unlink(missing_ok=True)

    def active_research_path(self, market_id: str) -> Path:
        """Return the path where active research for ``market_id`` lives."""
        return self._dirs["active"] / f"{market_id}.md"

    def read_active_research(self, market_id: str) -> str | None:
        """Return full markdown for ``02_Active_Research/{market_id}.md`` if present."""
        path = self.active_research_path(market_id)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def read_trade_log_dict(self, market_id: str) -> dict[str, Any] | None:
        """Parse ``03_Trades/{market_id}.json`` if present; return ``None`` on missing/bad JSON."""
        path = self._dirs["trades"] / f"{market_id}.json"
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return raw if isinstance(raw, dict) else None

    def post_mortem_path(self, market_id: str) -> Path:
        """Return the path where the post-mortem report for ``market_id`` lives."""
        return self._dirs["post_mortem"] / f"{market_id}.md"

    def read_post_mortem(self, market_id: str) -> str:
        """Read the post-mortem markdown for ``market_id``."""
        return self.post_mortem_path(market_id).read_text(encoding="utf-8")

    def read_trade_log(self, market_id: str) -> str:
        """Read the open trade log JSON for ``market_id`` as a raw string."""
        path = self._dirs["trades"] / f"{market_id}.json"
        return path.read_text(encoding="utf-8")

    def iter_post_mortems(self) -> list[Path]:
        """Return every post-mortem markdown file in ``04_Post_Mortems/``."""
        return sorted(self._dirs["post_mortem"].glob("*.md"))

    def iter_open_trades(self) -> list[Path]:
        """Return trade JSON files that have not yet been archived.

        Used by phase 5 to limit ``poly-scan get_market`` calls to markets
        that are still pending resolution. Archived trades live under
        ``03_Trades/_resolved/`` and are skipped.
        """
        return sorted(p for p in self._dirs["trades"].glob("*.json") if p.is_file())

    def _intent_path(self, market_id: str) -> Path:
        _check_market_id(market_id)
        return self._dirs["trades"] / "_intents" / f"{market_id}.json"

    def has_execution_intent(self, market_id: str) -> bool:
        """True when an execution intent record exists for ``market_id``."""
        return self._intent_path(market_id).exists()

    def write_execution_intent(self, market_id: str, record: dict[str, Any]) -> Path:
        """Persist an intent record before the Executioner is spawned (kept out of ``iter_open_trades``)."""
        path = self._intent_path(market_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, json.dumps(record, indent=2, default=str))
        return path

    def clear_execution_intent(self, market_id: str) -> None:
        """Remove the intent record (no-op if absent)."""
        self._intent_path(market_id).unlink(missing_ok=True)

    def archive_trade(self, market_id: str) -> Path:
        """Move ``03_Trades/{market_id}.json`` to ``03_Trades/_resolved/``.

        Called after the Post-Mortem Analyst successfully appends its
        analysis. Subsequent phase 5 ticks then skip this market, avoiding
        a polynomial blow-up of scraper calls against historical trades.
        """
        src = self._dirs["trades"] / f"{market_id}.json"
        if not src.exists():
            raise FileNotFoundError(f"No trade log to archive for {market_id}: {src}")
        dst = self._trades_archive_dir / src.name
        if dst.exists():
            dst = self._trades_archive_dir / f"{src.stem}__{_archive_timestamp()}{src.suffix}"
        shutil.move(str(src), str(dst))
        return dst

    # ------------------------------------------------------------------
    # File movement (orchestrator state transitions)
    # ------------------------------------------------------------------

    def move_file(
        self,
        market_id: str,
        src_dir_key: str,
        dst_dir_key: str,
    ) -> Path:
        """Move a market's file between Vault directories.

        Resolves the source by trying both ``.md`` and ``.json`` extensions.
        The destination filename keeps the same extension as the source.

        Common transitions:
        - ``"active"`` → ``"post_mortem"``  (market resolved)

        Parameters
        ----------
        market_id:
            Polymarket condition_id; used as the filename stem.
        src_dir_key:
            Key in ``VAULT_PATHS`` for the source directory.
        dst_dir_key:
            Key in ``VAULT_PATHS`` for the destination directory.

        Returns
        -------
        Path
            Absolute path of the file at its new location.

        Raises
        ------
        KeyError
            If ``src_dir_key`` or ``dst_dir_key`` is not a valid ``VAULT_PATHS`` key.
        FileNotFoundError
            If no file matching ``{market_id}.md`` or ``{market_id}.json``
            exists in the source directory.
        """
        _check_market_id(market_id)
        if src_dir_key not in self._dirs:
            raise KeyError(
                f"Unknown src_dir_key '{src_dir_key}'. "
                f"Valid keys: {list(self._dirs)}"
            )
        if dst_dir_key not in self._dirs:
            raise KeyError(
                f"Unknown dst_dir_key '{dst_dir_key}'. "
                f"Valid keys: {list(self._dirs)}"
            )

        src_dir = self._dirs[src_dir_key]
        dst_dir = self._dirs[dst_dir_key]

        # Resolve source file (try canonical extension first, then the other)
        primary_ext = _DIR_EXTENSIONS.get(src_dir_key, ".md")
        fallback_ext = ".json" if primary_ext == ".md" else ".md"
        src_file = src_dir / f"{market_id}{primary_ext}"
        if not src_file.exists():
            src_file = src_dir / f"{market_id}{fallback_ext}"
        if not src_file.exists():
            raise FileNotFoundError(
                f"No file found for market '{market_id}' in {src_dir}"
            )

        dst_file = dst_dir / src_file.name
        if dst_file.exists():
            raise FileExistsError(
                f"Refusing to overwrite existing destination for market '{market_id}': {dst_file}"
            )
        shutil.move(str(src_file), str(dst_file))
        return dst_file

    # ------------------------------------------------------------------
    # System helpers
    # ------------------------------------------------------------------

    def read_directives(self) -> str:
        """Return the raw contents of ``active_directives.md``.

        Returns
        -------
        str
            Full file contents, or an empty string if the file does not exist.
        """
        directives_path = self._dirs["system"] / _DIRECTIVES_FILENAME
        if not directives_path.exists():
            return ""
        return directives_path.read_text(encoding="utf-8")

    def cold_start_protocol(self) -> bool:
        """Write a seed ``active_directives.md`` if the file is missing or empty.

        The seed file embeds the neutral defaults from
        ``config/trading_constants.py`` so the Deep Researcher has a
        functional baseline before the Overseer completes its first learning
        loop.

        Returns
        -------
        bool
            ``True`` if the seed was written, ``False`` if a non-empty
            directives file already exists.
        """
        directives_path = self._dirs["system"] / _DIRECTIVES_FILENAME
        if directives_path.exists() and directives_path.stat().st_size > 0:
            return False

        seed_content = _build_seed_directives()
        _atomic_write_text(directives_path, seed_content)
        return True


# ---------------------------------------------------------------------------
# Cold Start seed builder (module-level helper, not part of public API)
# ---------------------------------------------------------------------------

def _build_seed_directives() -> str:
    """Return the full text of a seed ``active_directives.md``.

    Embeds live values from ``trading_constants`` so the document is always
    consistent with the rest of the configuration.
    """
    now_iso = datetime.now(tz=timezone.utc).isoformat()

    # Build a YAML-safe representation of the FILTERS dict
    filters_yaml_lines = "\n".join(
        f"    {key}: {value}" for key, value in FILTERS.items()
    )

    frontmatter_block = f"""\
---
version: "0.1-seed"
seeded_at: "{now_iso}"
overseer_updated: false
---"""

    body = f"""\
# Active Directives for the Deep Researcher

> **NOTE — COLD START SEED**
> This file was auto-generated by the Cold Start Protocol because no prior
> directives existed.  It will be overwritten the first time the Overseer
> completes a learning loop.

---

## Research Protocol

You are a fundamental analyst evaluating binary prediction markets on
Polymarket.  For every market assigned to you, apply the following
standard methodology before forming your probability estimate (`estimated_p`):

1. **News Sentiment** — Search for recent news (last 7 days) regarding the
   market subject.  Weight negative sentiment down, positive sentiment up,
   but penalise thin coverage regardless of direction.

2. **Upcoming Catalysts** — Identify scheduled events (earnings, elections,
   regulatory decisions, economic releases) within the market's resolution
   window.  Flag catalysts that could sharply shift the probability.

3. **Liquidity & Market Health** — Check whether the market has sufficient
   liquidity (see `low_liquidity_breakout_max_liq` below).  Thin markets
   carry higher spread risk; apply a conservatism discount to `estimated_p`
   for markets below the threshold.

4. **Base Rate Anchoring** — Start from a calibrated base rate for the
   event class (e.g., incumbent re-election rates, historical merger
   completion rates) before adjusting for specific evidence.

5. **Thesis Balance** — You MUST produce both a Bull Thesis and a Bear
   Thesis of comparable depth.  Asymmetric output (one section trivially
   short) is a signal of anchoring bias.

---

## Filter Weightings (Neutral Defaults)

The Quantitative Filter uses the thresholds below.  Do NOT override these
in your analysis — they are deterministic guard-rails applied before you
are invoked.

```yaml
filters:
{filters_yaml_lines}
```

---

## Risk Constraints

The following constants are active for this session.  Respect them in your
qualitative reasoning:

| Parameter | Value | Meaning |
|---|---|---|
| `F_MAX` | `{F_MAX}` | Hard cap on fraction of bankroll per position |
| `PAPER_TRADE_MODE` | `{PAPER_TRADE_MODE}` | Live API disabled — all trades are simulated |

When `PAPER_TRADE_MODE` is `True`, you may be slightly more exploratory
in borderline cases since no real capital is at risk.

---

## Output Requirements

You MUST return your response in the exact format specified by the system:

```
---
market_id: "<id>"
estimated_p: <float between 0.0 and 1.0>
error: null
---

## Bull Thesis

<analysis>

## Bear Thesis

<analysis>

## Post-Mortem
```

Leave the `## Post-Mortem` section empty.  The Orchestrator will populate
it after the market resolves.

---

*This seed is active until the Overseer completes its first learning loop.*
"""

    return frontmatter_block + "\n\n" + body
