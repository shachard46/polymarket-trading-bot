"""Hub-side validation of agent inputs and outputs against ``agent.yaml``.

The OpenClaw cloud SDK does not advertise its registered schemas back to the
caller, so the local repository is the only source of truth. ``spawn_agent``
calls into this module to:

1. Validate the payload before it ever leaves the Hub.
2. Validate the agent's response after it returns.

Mismatches raise :class:`AgentSchemaError` so the orchestrator can DLQ the
market with a precise reason.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Type, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, ValidationError, create_model

# Sentinel used by ``deep_researcher`` instead of a dict schema.
MARKDOWN_OUTPUT = "markdown_with_yaml_frontmatter"

_NULLABLE_RE = re.compile(r"^(?P<inner>.+?)\s*\|\s*null$", re.IGNORECASE)

_PRIMITIVES: dict[str, Any] = {
    "string": str,
    "str": str,
    "boolean": bool,
    "bool": bool,
    "float": float,
    "int": int,
    "integer": int,
    "dict": dict,
    "list": list,
    "list[dict]": list[dict],
    "list[str]": list[str],
    "list[float]": list[float],
    "any": Any,
}


class AgentSchemaError(ValueError):
    """Raised when a payload fails Hub-side schema validation."""

    def __init__(self, role: str, direction: str, cause: ValidationError | str) -> None:
        self.role = role
        self.direction = direction
        self.cause = cause
        super().__init__(f"{role} {direction} schema mismatch: {cause}")


def _resolve_type(decl: Any) -> Any:
    if not isinstance(decl, str):
        # Allow callers to pre-resolve to a Python type.
        return decl if decl is not None else Any
    text = decl.strip()
    if not text:
        return Any
    nullable = _NULLABLE_RE.match(text)
    if nullable:
        return Optional[_resolve_type(nullable.group("inner").strip())]
    return _PRIMITIVES.get(text, Any)


def build_model(name: str, schema: Any) -> Optional[Type[BaseModel]]:
    """Build a Pydantic model for ``schema`` or return ``None`` for non-dict schemas.

    String schemas (``"markdown_with_yaml_frontmatter"``) are signalled by
    returning ``None``; the caller must handle them out-of-band.
    """
    if not isinstance(schema, dict) or not schema:
        return None
    fields: dict[str, tuple[Any, Any]] = {}
    for key, decl in schema.items():
        py_type = _resolve_type(decl)
        fields[key] = (py_type, ...)
    return create_model(  # type: ignore[call-overload]
        name,
        __config__=ConfigDict(extra="allow"),
        **fields,
    )


def validate_payload(
    role: str,
    direction: str,
    model: Optional[Type[BaseModel]],
    payload: Any,
) -> None:
    """Validate ``payload`` against ``model`` (no-op if ``model`` is ``None``)."""
    if model is None:
        return
    if not isinstance(payload, dict):
        raise AgentSchemaError(
            role, direction, f"expected mapping, got {type(payload).__name__}"
        )
    try:
        model.model_validate(payload)
    except ValidationError as exc:
        raise AgentSchemaError(role, direction, exc) from exc


def is_markdown_output(schema: Any) -> bool:
    return isinstance(schema, str) and schema.strip() == MARKDOWN_OUTPUT


__all__ = [
    "MARKDOWN_OUTPUT",
    "AgentSchemaError",
    "build_model",
    "validate_payload",
    "is_markdown_output",
]
