"""Pydantic models for Phase 3 agent JSON contracts."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

DeepResearcherOutput = Union["DeepResearcherNeedsMore", "DeepResearcherComplete"]


BRIEFER_QUERY_SLOTS = 4  # resolution/polarity, base rate, current state, disconfirming evidence


class BrieferOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    market_id: str
    research_queries: list[str]
    error: str | None = None

    @field_validator("research_queries")
    @classmethod
    def _clean_queries(cls, value: list[str]) -> list[str]:
        return [str(q).strip() for q in value if str(q).strip()]

    @model_validator(mode="after")
    def _error_or_four_queries(self) -> "BrieferOutput":
        if self.error:
            return self
        if len(self.research_queries) != BRIEFER_QUERY_SLOTS:
            raise ValueError(
                f"research_queries must contain exactly {BRIEFER_QUERY_SLOTS} non-empty "
                "strings (fixed slots) when error is null"
            )
        return self


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

    @field_validator("estimated_p", "p_low", "p_high")
    @classmethod
    def _p_in_unit_interval(cls, value: float) -> float:
        if not (0.0 <= value <= 1.0):
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
    "BRIEFER_QUERY_SLOTS",
    "BrieferOutput",
    "DeepResearcherComplete",
    "DeepResearcherNeedsMore",
    "DeepResearcherOutput",
    "parse_deep_researcher_json",
]
