"""Pydantic models for Phase 3 agent JSON contracts."""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

DeepResearcherOutput = Union["DeepResearcherNeedsMore", "DeepResearcherComplete"]


class BrieferOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    market_id: str
    research_queries: list[str]
    error: str | None = None

    @field_validator("research_queries")
    @classmethod
    def _one_to_three_queries(cls, value: list[str]) -> list[str]:
        cleaned = [str(q).strip() for q in value if str(q).strip()]
        if len(cleaned) > 3:
            raise ValueError("research_queries must contain at most 3 non-empty strings")
        return cleaned

    @model_validator(mode="after")
    def _error_or_nonempty_queries(self) -> "BrieferOutput":
        if self.error:
            return self
        if not self.research_queries:
            raise ValueError(
                "research_queries must contain 1 to 3 non-empty strings when error is null"
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
    """Complete Forensic Fact Verifier payload.

    ``markdown`` is the full active-research wire document: YAML frontmatter plus
    Bull/Bear thesis sections (2-3 max-density bullets each) and empty Post-Mortem.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["complete"]
    market_id: str
    estimated_p: float
    markdown: str

    @field_validator("estimated_p")
    @classmethod
    def _p_in_unit_interval(cls, value: float) -> float:
        if not (0.0 <= value <= 1.0):
            raise ValueError(f"estimated_p must be in [0, 1], got {value}")
        return value


def parse_deep_researcher_json(parsed: dict[str, Any]) -> DeepResearcherOutput:
    """Validate a Deep Researcher state-machine payload by ``status``."""
    status = parsed.get("status")
    if status == "needs_more_data":
        return DeepResearcherNeedsMore.model_validate(parsed)
    if status == "complete":
        return DeepResearcherComplete.model_validate(parsed)
    raise ValidationError.from_exception_data(
        "DeepResearcherOutput",
        [
            {
                "type": "literal_error",
                "loc": ("status",),
                "msg": f"unexpected status {status!r}",
                "input": status,
            }
        ],
    )


__all__ = [
    "BrieferOutput",
    "DeepResearcherComplete",
    "DeepResearcherNeedsMore",
    "DeepResearcherOutput",
    "parse_deep_researcher_json",
]
