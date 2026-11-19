"""Vault schemas: error contract, atomic writers, and Pydantic payload models."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ValidationError, field_validator, model_validator



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


# Process umask, read once (os.umask can only be read by setting it). mkstemp always creates 0600;
# atomic writes re-apply the normal 0666 & ~umask mode so the vault stays group-readable.
_UMASK = os.umask(0)
os.umask(_UMASK)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a same-dir temp file, fsync, and ``os.replace``."""
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o666 & ~_UMASK)
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
    """Phase 2 gate/triage state for one market — written to 01_Filters/.

    ``passed`` = promoted to Phase 3; ``confidence_multiplier`` mirrors the gate
    ``score`` (Phase 3 ranks on it). ``fail_count``/``next_check_at``/``last_q``
    are the gate backoff state; ``triage`` is the cached Triage verdict.
    """

    market_id: str
    passed: bool
    trigger: Optional[str] = None
    confidence_multiplier: float
    details: str
    error: Optional[str] = None
    status: Optional[str] = None
    error_log: Optional[dict[str, Any]] = None
    score: Optional[float] = None
    event_id: Optional[str] = None
    fail_count: int = 0
    next_check_at: Optional[str] = None
    last_q: Optional[float] = None
    triage: Optional[dict[str, Any]] = None
    # Phase 3 bookkeeping (Hub-side): last research attempt (stage/outcome/p/range/q/at)
    # and a pending refresh trigger set by Phase 2 ("price_move").
    research_state: Optional[dict[str, Any]] = None
    refresh_reason: Optional[str] = None
    # Market integrity: first description hash ever seen (set once, never overwritten) and the
    # latest score/flags/key numeric features.
    description_first_hash: Optional[str] = None
    integrity: Optional[dict[str, Any]] = None


class ResearchFrontmatter(BaseModel):
    """YAML frontmatter of an active research report — written to 02_Active_Research/.

    ``estimated_p`` / ``p_low`` / ``p_high`` are the aggregated FULL-stage values.
    ``q_at_research`` is the market price when the research ran; it is Hub-side
    bookkeeping (refresh trigger) and is never sent to a model.
    """

    market_id: str
    estimated_p: float
    error: Optional[str] = None
    p_low: Optional[float] = None
    p_high: Optional[float] = None
    confidence: Optional[str] = None
    as_of: Optional[str] = None
    resolution_checklist: list[str] = []
    key_assumptions: list[str] = []
    q_at_research: Optional[float] = None
    researched_at: Optional[str] = None
    end_date: Optional[str] = None
    days_at_research: Optional[float] = None
    samples: list[float] = []
    evidence_source: Optional[str] = None
    evidence_fallback: Optional[bool] = None
    pipeline: Optional[str] = None  # "crux_v1": question-led FULL stage
    research_depth: Optional[str] = None  # "lite" | "full" (stake-scaled)
    cruxes_total: Optional[int] = None
    crux_answered: Optional[int] = None
    crux_unanswered: Optional[int] = None
    crux_contradicted: Optional[int] = None
    gap_queries: Optional[int] = None
    audit_dropped: Optional[int] = None
    audit_unverified: Optional[int] = None
    framing_gap: Optional[float] = None  # |J1 - flipped J2|
    source_mix: dict[str, int] = {}
    pipeline_flags: list[str] = []  # degraded stages (scout_failed, gap_failed, audit_failed, judge_failed:j2, ...)
    web_calls: Optional[int] = None
    judge_arm: Optional[str] = None  # "one" | "two" (judge cost A/B)
    judge_framing: Optional[str] = None  # framing of the single judge ("yes" | "no"); null with two judges
    compound_type: Optional[str] = None  # "and" | "or" for a compound market
    compound_p: Optional[float] = None  # P(YES) combined in code from the judge's component probabilities
    compound_stated_p: Optional[float] = None  # the judge's stated P(YES) for the same sample(s)
    compound_gap: Optional[float] = None
    compound_corr: Optional[float] = None
    prior: Optional[float] = None  # judge's starting point before case evidence (P(YES), J1)
    prior_kind: Optional[str] = None  # "reference_class" | "structural" | "judgment"
    base_rate: Optional[float] = None  # P(YES) from real reference-class counts only, else null
    base_rate_n: Optional[int] = None  # past cases behind base_rate
    prior_j2: Optional[float] = None  # J2's prior converted to P(YES), when both judges ran
    prior_kind_j2: Optional[str] = None
    discounted_sources: list[str] = []  # interested / unproven sources the judge discounted
    evidence_bull: list[str] = []  # verbatim audit-verified lines the judge cited for YES (offer evidence)
    evidence_bear: list[str] = []  # ... and for NO
    status: Optional[str] = None
    error_log: Optional[dict[str, Any]] = None

    @field_validator("estimated_p", "p_low", "p_high")
    @classmethod
    def _p_in_unit_interval(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError(f"probability must be in [0, 1], got {v}")
        return v

    @model_validator(mode="after")
    def _range_brackets_estimate(self) -> "ResearchFrontmatter":
        if self.p_low is not None and self.p_high is not None:
            if not (self.p_low - 1e-9 <= self.estimated_p <= self.p_high + 1e-9):
                raise ValueError("expected p_low <= estimated_p <= p_high")
        return self


class TradeLogPayload(BaseModel):
    """Hub-built trade record (allocation + execute_polymarket_trade result) — written to 03_Trades/."""

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
    ask: Optional[float] = None  # executable best ask of the bought side (CLOB) at decision time
    depth_usd: Optional[float] = None  # ask depth within 2c of that ask, USD
    directive_version: Optional[int] = None
    event_id: Optional[str] = None


FAILURE_CLASSES = frozenset(
    {"bad_info", "bad_reasoning", "resolution_misread", "variance", "correct"}
)


class PostMortemPayload(BaseModel):
    """Post-Mortem Analyst output — appended under ## Post-Mortem in 04_Post_Mortems/."""

    market_id: str
    post_mortem_analysis: str
    failure_class: Optional[str] = None
    error: Optional[str] = None
    status: Optional[str] = None
    error_log: Optional[dict[str, Any]] = None

    @field_validator("failure_class")
    @classmethod
    def _validate_failure_class(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip().lower()
        from orchestrator.failure_tags import TAG_PREFIX, UNCLASSIFIED, SLUG_RE  # lazy: avoids an import cycle

        if cleaned in FAILURE_CLASSES or cleaned == UNCLASSIFIED:
            return cleaned
        if cleaned.startswith(TAG_PREFIX) and SLUG_RE.match(cleaned[len(TAG_PREFIX):]):
            return cleaned
        raise ValueError(
            f"failure_class must be one of {sorted(FAILURE_CLASSES)}, 'unclassified' or 'new:<slug>', got {value!r}"
        )


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


def read_json(path: Path) -> Any | None:
    """Return parsed JSON from ``path``, or ``None`` if missing or unreadable."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json_atomic(
    path: Path, obj: Any, *, indent: int = 2, sort_keys: bool = False, default: Any = None
) -> None:
    """Write ``obj`` as JSON to ``path`` atomically, creating the parent directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(obj, indent=indent, sort_keys=sort_keys, default=default))
