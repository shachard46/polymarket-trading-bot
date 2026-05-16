"""
ObsidianManager — Pydantic-gated file system layer for the Obsidian Vault.

The Orchestrator is the ONLY caller.  Agents never touch the file system
directly; they return raw dicts that are validated here before any write
is committed.  A ValidationError raises VaultWriteError, which the
Orchestrator catches to route the market into the Dead Letter Queue.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, ValidationError, field_validator

from config.trading_constants import (
    ALPHA,
    BETA,
    F_MAX,
    FILTERS,
    PAPER_TRADE_MODE,
    S_0,
    VAULT_PATHS,
)
from config.vault import resolve_vault_base

# ---------------------------------------------------------------------------
# Error contract
# ---------------------------------------------------------------------------

class VaultWriteError(Exception):
    """Raised when an agent payload fails Pydantic validation.

    The Orchestrator must catch this, call write_error_log, and skip the
    current market — never re-raise into the outer pipeline loop.
    """

    def __init__(self, market_id: str, cause: ValidationError) -> None:
        self.market_id = market_id
        self.cause = cause
        super().__init__(
            f"Vault write aborted for market '{market_id}': {cause}"
        )


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


class ResearchFrontmatter(BaseModel):
    """YAML frontmatter block from the Deep Researcher — written to 02_Active_Research/."""

    market_id: str
    estimated_p: float
    error: Optional[str] = None
    edge_research_refresh_count: int = 0

    @field_validator("estimated_p")
    @classmethod
    def _p_in_unit_interval(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"estimated_p must be in [0, 1], got {v}")
        return v

    @field_validator("edge_research_refresh_count")
    @classmethod
    def _edge_refresh_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("edge_research_refresh_count must be >= 0")
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


class PostMortemPayload(BaseModel):
    """Post-Mortem Analyst output — appended under ## Post-Mortem in 04_Post_Mortems/."""

    market_id: str
    post_mortem_analysis: str
    error: Optional[str] = None


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
        return value


# ---------------------------------------------------------------------------
# ObsidianManager
# ---------------------------------------------------------------------------

_DIRECTIVES_FILENAME = "active_directives.md"


def _dlq_timestamp() -> str:
    """Return a filesystem-safe UTC timestamp suffix for DLQ artifacts.

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
    fm = yaml.dump(data, default_flow_style=False, allow_unicode=True).rstrip()
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
    "errors": ".json",
}


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
        target.write_text(content, encoding="utf-8")
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
        try:
            validated = FilterLogPayload.model_validate(payload)
        except ValidationError as exc:
            raise VaultWriteError(market_id, exc) from exc

        dest = self._dirs["filters"] / f"{market_id}.md"
        dest.write_text(
            _dump_frontmatter(validated.model_dump()), encoding="utf-8"
        )
        return dest

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
        try:
            validated = ResearchFrontmatter.model_validate(payload)
        except ValidationError as exc:
            raise VaultWriteError(market_id, exc) from exc

        dest = self._dirs["active"] / f"{market_id}.md"
        dest.write_text(
            _dump_frontmatter(validated.model_dump(), body), encoding="utf-8"
        )
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
        try:
            validated = TradeLogPayload.model_validate(payload)
        except ValidationError as exc:
            raise VaultWriteError(market_id, exc) from exc

        dest = self._dirs["trades"] / f"{market_id}.json"
        dest.write_text(
            json.dumps(validated.model_dump(), indent=2), encoding="utf-8"
        )
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
        header = "## Post-Mortem"
        if header in content:
            insert_at = content.index(header) + len(header)
            content = (
                content[:insert_at]
                + "\n\n"
                + validated.post_mortem_analysis
                + content[insert_at:]
            )
        else:
            content += f"\n\n{header}\n\n{validated.post_mortem_analysis}\n"

        dest.write_text(content, encoding="utf-8")
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
        dest.write_text(validated.new_directives_markdown, encoding="utf-8")
        return dest

    def write_error_log(
        self,
        market_id: str,
        payload: dict,
        reason: str,
    ) -> Path:
        """Write an error entry to the Dead Letter Queue (``05_Errors/``).

        This method never raises on payload content — it is the last resort
        and must always succeed to avoid swallowing pipeline errors silently.

        Parameters
        ----------
        market_id:
            Polymarket condition_id; used as the filename stem.
        payload:
            The raw dict that caused the failure (may be malformed).
        reason:
            Human-readable description of why the market was rejected.

        Returns
        -------
        Path
            Absolute path of the written error file.
        """
        suffix = _dlq_timestamp()
        error_record = {
            "market_id": market_id,
            "logged_at": datetime.now(tz=timezone.utc).isoformat(),
            "reason": reason,
            "payload": payload,
        }
        dest = self._dirs["errors"] / f"{market_id}__{suffix}.json"
        dest.write_text(json.dumps(error_record, indent=2), encoding="utf-8")
        return dest

    def iter_error_logs(self, market_id: str) -> list[Path]:
        """Return every DLQ artifact recorded for ``market_id``, oldest-first."""
        return sorted(self._dirs["errors"].glob(f"{market_id}__*.json"))

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
            dst = self._trades_archive_dir / f"{src.stem}__{_dlq_timestamp()}{src.suffix}"
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
        - any key → ``"errors"``            (dead letter queue)

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
        # DLQ moves must never overwrite a prior failure's artifact for the
        # same market; suffix with a UTC timestamp on collision.
        if dst_dir_key == "errors" and dst_file.exists():
            stem, ext = src_file.stem, src_file.suffix
            dst_file = dst_dir / f"{stem}__{_dlq_timestamp()}{ext}"
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
        directives_path.write_text(seed_content, encoding="utf-8")
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
| `BETA` | `{BETA}` | Kelly fraction dampener — never recommend full-Kelly sizing |
| `F_MAX` | `{F_MAX}` | Hard cap on fraction of bankroll per position |
| `ALPHA` | `{ALPHA}` | Minimum score scalar for trade to be placed |
| `S_0` | `{S_0}` | Minimum edge threshold |
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
