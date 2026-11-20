"""Pydantic models for Phase 3 agent JSON contracts."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

DeepResearcherOutput = Union["DeepResearcherNeedsMore", "DeepResearcherComplete"]


BRIEFER_QUERY_SLOTS = 4  # legacy fixed-slot briefer output (resolution/polarity, base rate, current state, disconfirming)
CRUX_ROLES = ("mechanism", "comparator", "evidence", "horizon", "skeptic")
REQUIRED_CRUX_ROLES = ("mechanism", "comparator", "skeptic", "horizon")
CRUX_STATUSES = ("answered", "unanswered", "contradicted")


class CruxSpec(BaseModel):
    """One research question from the architect: role, question, 1-2 queries, what moves p."""

    model_config = ConfigDict(extra="allow")

    role: Literal["mechanism", "comparator", "evidence", "horizon", "skeptic"]
    question: str
    queries: list[str]
    moves_p_if: str = ""
    component: str | None = None  # compound market: id of the component this crux is about
    source: Literal["rules", "web"] = "web"  # "rules": answered by the market rules text itself (not searched)
    queries_local: list[str] = []  # non-English market locale: one local-language query (roles evidence/comparator/horizon)
    provenance_question: str = ""  # who produced the forecast/estimate this crux relies on, their stake, track record

    @field_validator("question", "moves_p_if", "provenance_question")
    @classmethod
    def _strip(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("queries")
    @classmethod
    def _clean_queries(cls, value: list[str]) -> list[str]:
        return [str(q).strip() for q in value if str(q).strip()]

    @model_validator(mode="after")
    def _has_question_and_queries(self) -> "CruxSpec":
        if not self.question:
            raise ValueError("crux question must be non-empty")
        if not 1 <= len(self.queries) <= 2:
            raise ValueError("crux queries must contain 1 to 2 non-empty strings")
        return self


class BrieferOutput(BaseModel):
    """Architect output: ``cruxes``; the legacy four fixed-slot ``research_queries`` is still accepted."""

    model_config = ConfigDict(extra="allow")

    market_id: str
    cruxes: list[CruxSpec] = []
    locale: dict[str, Any] | None = None  # {"country", "language"}: the architect's own, used only when the scout gave none
    compound: dict[str, Any] | None = None  # {"type": "and"|"or", "components": [{"id", "label"}]} for compound markets
    research_queries: list[str] = []
    error: str | None = None

    @field_validator("research_queries")
    @classmethod
    def _clean_queries(cls, value: list[str]) -> list[str]:
        return [str(q).strip() for q in value if str(q).strip()]

    @model_validator(mode="after")
    def _error_or_cruxes_or_four_queries(self) -> "BrieferOutput":
        if self.error or self.cruxes:
            return self
        if len(self.research_queries) != BRIEFER_QUERY_SLOTS:
            raise ValueError(
                "briefer must return non-empty cruxes (or, legacy, exactly "
                f"{BRIEFER_QUERY_SLOTS} research_queries) when error is null"
            )
        return self


class ScoutOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    market_id: str
    event: str = ""
    deciding_actors: list[str] = []
    resolution_source: str | None = None
    scheduled: list[str] = []
    contested: list[str] = []
    main_sources: list[str] = []
    locale: dict[str, Any] | None = None  # {"country": ISO-2 or null, "language": e.g. "zh-hant" | "en"}
    error: str | None = None


class SourceTier(BaseModel):
    model_config = ConfigDict(extra="allow")

    domain: str
    tier: str = "unknown"


class SourceClassifierOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    market_id: str = ""
    domains: list[SourceTier] = []
    error: str | None = None


class ComponentP(BaseModel):
    """Judge output for a compound market: probability that one component happens (independent of ``ask``)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    p: float

    @field_validator("p")
    @classmethod
    def _unit(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"component probability must be in [0, 1], got {value}")
        return value


class CruxAssessment(BaseModel):
    model_config = ConfigDict(extra="allow")

    crux_id: str
    status: Literal["answered", "unanswered", "contradicted"]
    missing: str = ""


class FollowUpQuery(BaseModel):
    model_config = ConfigDict(extra="allow")

    crux_id: str
    query: str


class GapOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    market_id: str
    assessments: list[CruxAssessment] = []
    follow_up_queries: list[FollowUpQuery] = []
    error: str | None = None


class AuditVerdict(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    supported: bool
    reason: str = ""


class AuditOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    market_id: str
    verdicts: list[AuditVerdict] = []
    error: str | None = None


class CruxVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    crux_id: str
    direction: Literal["yes", "no", "neutral"] = "neutral"
    cited: list[str] = []
    note: str = ""


class DeepResearcherNeedsMore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["needs_more_data"]
    new_queries: list[str]

    @field_validator("new_queries")
    @classmethod
    def _one_to_three_queries(cls, value: list[str]) -> list[str]:
        cleaned = [str(q).strip() for q in value if str(q).strip()]
        if not cleaned or len(cleaned) > 3:
            raise ValueError("new_queries must contain 1 to 3 non-empty strings")
        return cleaned


class DeepResearcherComplete(BaseModel):
    """Complete Forensic Fact Verifier payload (price-blind: the model never sees q).

    ``markdown`` is the full active-research wire document: YAML frontmatter plus
    Bull/Bear thesis sections (2-3 max-density bullets each) and empty Post-Mortem.
    ``p_low <= estimated_p <= p_high``; an inconclusive study reports a wide range.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["complete"]
    market_id: str
    estimated_p: float
    p_low: float
    p_high: float
    confidence: Literal["low", "medium", "high"]
    resolution_checklist: list[str]
    key_assumptions: list[str]
    as_of: date
    markdown: str
    error: str | None = None
    base_rate: float | None = None  # judge mode: reference-class rate for the asked outcome; only from real counts
    base_rate_n: int | None = None  # judge mode: number of past cases behind base_rate
    base_rate_source: str | None = None  # judge mode: evidence id (E7) holding those counts
    prior: float | None = None  # judge mode: starting point before case evidence (normalised in p3_research.prior_fields)
    prior_kind: str | None = None  # judge mode: reference_class | structural | judgment (anything else -> judgment)
    crux_verdicts: list[CruxVerdict] = []  # judge mode: which way each crux pushes, with cited E-ids
    components: list[ComponentP] = []  # judge mode, compound market: P(component happens) per component id
    correlation: float | None = None  # judge mode, compound market: assumed correlation between the components
    discounted_sources: list[str] = []  # judge mode: interested / unproven sources whose claims it discounted

    @field_validator("estimated_p", "p_low", "p_high", "base_rate", "prior")
    @classmethod
    def _p_in_unit_interval(cls, value: float | None) -> float | None:
        if value is not None and not (0.0 <= value <= 1.0):
            raise ValueError(f"probability must be in [0, 1], got {value}")
        return value

    @model_validator(mode="after")
    def _range_brackets_estimate(self) -> "DeepResearcherComplete":
        if not (self.p_low - 1e-9 <= self.estimated_p <= self.p_high + 1e-9):
            raise ValueError(
                f"expected p_low <= estimated_p <= p_high, got "
                f"{self.p_low} / {self.estimated_p} / {self.p_high}"
            )
        return self


def _validation_error(message: str, loc: tuple[str, ...], value: Any) -> ValidationError:
    return ValidationError.from_exception_data(
        "DeepResearcherOutput",
        [
            {
                "type": "value_error",
                "loc": loc,
                "msg": message,
                "input": value,
                "ctx": {"error": ValueError(message)},
            }
        ],
    )


def parse_deep_researcher_json(parsed: dict[str, Any]) -> DeepResearcherOutput:
    """Validate a Deep Researcher state-machine payload by ``status``.

    A non-empty ``error`` is surfaced (as a :class:`ValidationError` carrying the
    agent's message) before ``estimated_p`` / ``markdown`` are validated.
    """
    err = parsed.get("error")
    if err is not None and str(err).strip():
        raise _validation_error(
            f"deep researcher reported error: {str(err).strip()}", ("error",), err
        )
    status = parsed.get("status")
    if status == "needs_more_data":
        return DeepResearcherNeedsMore.model_validate(
            {k: v for k, v in parsed.items() if k != "error"}
        )
    if status == "complete":
        return DeepResearcherComplete.model_validate(parsed)
    raise _validation_error(f"unexpected status {status!r}", ("status",), status)


__all__ = [
    "AuditOutput",
    "BRIEFER_QUERY_SLOTS",
    "BrieferOutput",
    "CRUX_ROLES",
    "CRUX_STATUSES",
    "CruxSpec",
    "CruxVerdict",
    "GapOutput",
    "REQUIRED_CRUX_ROLES",
    "ScoutOutput",
    "ComponentP",
    "SourceClassifierOutput",
    "DeepResearcherComplete",
    "DeepResearcherNeedsMore",
    "DeepResearcherOutput",
    "parse_deep_researcher_json",
]
