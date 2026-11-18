# ruff: noqa: F401
"""Vault I/O package; re-exports the public and underscore names used across the repo."""

from .directives import _DIRECTIVES_FILENAME, _build_seed_directives
from .manager import ObsidianManager, _archive_timestamp
from .research_bundle import (
    _has_usable_research_data,
    _merge_research_bundle_queries,
    _should_replace_bundle_entry,
)
from .schemas import (
    FAILURE_CLASSES,
    DirectivesPayload,
    FilterLogPayload,
    PostMortemPayload,
    ResearchFrontmatter,
    TradeLogPayload,
    VaultWriteError,
    _atomic_write_text,
    _check_market_id,
    _dump_frontmatter,
    read_json,
    write_json_atomic,
)

__all__ = [
    "DirectivesPayload",
    "FAILURE_CLASSES",
    "FilterLogPayload",
    "ObsidianManager",
    "PostMortemPayload",
    "ResearchFrontmatter",
    "TradeLogPayload",
    "VaultWriteError",
    "read_json",
    "write_json_atomic",
]
