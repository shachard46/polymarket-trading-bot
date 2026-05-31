"""Schema-driven live prompts for OpenClaw agent invocations."""

from __future__ import annotations

from typing import Any


def build_live_response_hint(output_schema: dict[str, Any]) -> str:
    """Build response instructions from ``agent.yaml`` ``output_schema``."""
    if not output_schema:
        return (
            "Return only the structured output required by your output contract."
        )

    keys = sorted(output_schema)
    key_list = ", ".join(keys)
    nullable = [
        name
        for name, decl in output_schema.items()
        if "null" in str(decl).lower()
    ]

    lines = [
        "Return ONLY a raw JSON object (final response after any tool calls in AGENTS.md).",
        "First character must be `{`, last must be `}`.",
        f"Required keys exactly: {key_list}.",
    ]
    if nullable:
        nullable_list = ", ".join(sorted(nullable))
        lines.append(
            f"Always include nullable fields ({nullable_list}) — use JSON null, "
            "never omit the key."
        )
    lines.append(
        "Do not include keys not listed above (e.g. signal_bundle — the Hub "
        "attaches it after evaluator runs)."
    )
    if "market_id" in output_schema:
        lines.append("Copy market_id from the input JSON when present in input.")
    lines.append("No prose outside the JSON object, no markdown fences around it.")
    return "\n".join(lines)


__all__ = ["build_live_response_hint"]
