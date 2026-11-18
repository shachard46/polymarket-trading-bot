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
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from config.trading_constants import STATUS_INACTIVE, STATUS_KEY, VAULT_PATHS
from config.vault import resolve_vault_base

from .directives import DirectivesMixin
from .research_bundle import ResearchBundleMixin
from .schemas import (
    FilterLogPayload,
    PostMortemPayload,
    ResearchFrontmatter,
    TradeLogPayload,
    VaultWriteError,
    _atomic_write_text,
    _check_market_id,
    _dump_frontmatter,
)

log = logging.getLogger(__name__)


def _archive_timestamp() -> str:
    """Return a filesystem-safe UTC timestamp suffix for archived trade filenames.

    ISO 8601 with ``:`` would be illegal on Windows, so we collapse the
    time portion to a continuous digit string with microsecond resolution.
    """
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


# Extension used per directory when the archive format is not JSON
_DIR_EXTENSIONS: dict[str, str] = {
    "system": ".md",
    "filters": ".md",
    "active": ".md",
    "trades": ".json",
    "post_mortem": ".md",
}

_FRONTMATTER_DIR_KEYS: frozenset[str] = frozenset({"filters", "active", "post_mortem"})


def _resolve_src(src_dir: Path, src_dir_key: str, market_id: str) -> Path:
    """Find ``{market_id}`` in ``src_dir``: canonical extension first, then the other."""
    primary_ext = _DIR_EXTENSIONS.get(src_dir_key, ".md")
    fallback_ext = ".json" if primary_ext == ".md" else ".md"
    src_file = src_dir / f"{market_id}{primary_ext}"
    if not src_file.exists():
        src_file = src_dir / f"{market_id}{fallback_ext}"
    if not src_file.exists():
        raise FileNotFoundError(f"No file found for market '{market_id}' in {src_dir}")
    return src_file


def _render_post_mortem_entry(content: str, analysis: str) -> str:
    """Place ``analysis`` under the ``## Post-Mortem`` header (replace the section, or append one)."""
    match = re.compile(r"^## Post-Mortem[ \t]*$", re.MULTILINE).search(content)
    if not match:
        return content.rstrip("\n") + f"\n\n## Post-Mortem\n\n{analysis}\n"
    # Section runs to the next level-2 header (or EOF); replace its content.
    next_h = re.compile(r"^## ", re.MULTILINE).search(content, match.end())
    tail = content[next_h.start():] if next_h else ""
    content = content[: match.end()] + "\n\n" + analysis + "\n"
    if tail:
        content += "\n" + tail
    return content


class ObsidianManager(DirectivesMixin, ResearchBundleMixin):
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
        """Validate a Phase 2 filter payload and write it to ``01_Filters/``.

        The file is written as a Markdown document whose YAML frontmatter
        contains the entire validated payload as a JSON-compatible block,
        matching the spec: *pure JSON (or .md with only YAML frontmatter)*.

        Parameters
        ----------
        market_id:
            Polymarket condition_id; used as the filename stem.
        payload:
            Gate/triage state dict (see ``FilterLogPayload``).

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

        Used by Phase 2 to carry backoff and triage state across ticks.
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
        """Validate a trade payload and write to ``03_Trades/``.

        The file is written as a pretty-printed JSON document.

        Parameters
        ----------
        market_id:
            Polymarket condition_id; used as the filename stem.
        payload:
            Trade dict built by the Hub (allocation + trade skill result).

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
        content = _render_post_mortem_entry(content, validated.post_mortem_analysis.strip())

        if validated.failure_class:
            from orchestrator.research import split_yaml_frontmatter_markdown

            try:
                fm, body = split_yaml_frontmatter_markdown(content)
            except ValueError:
                pass  # no frontmatter to patch; the ledger row still carries failure_class
            else:
                fm["failure_class"] = validated.failure_class
                content = _dump_frontmatter(fm, body)
        _atomic_write_text(dest, content)
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
        """Persist an intent record before the trade skill is called (kept out of ``iter_open_trades``)."""
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

        src_file = _resolve_src(src_dir, src_dir_key, market_id)

        dst_file = dst_dir / src_file.name
        if dst_file.exists():
            raise FileExistsError(
                f"Refusing to overwrite existing destination for market '{market_id}': {dst_file}"
            )
        shutil.move(str(src_file), str(dst_file))
        return dst_file
