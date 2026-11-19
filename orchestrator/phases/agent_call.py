"""Shared agent-call plumbing: runner call -> parse -> schema-validate -> flag on error."""

from __future__ import annotations

from typing import Any, Callable

from obsidian_utils import ObsidianManager
import agents_blueprint  # module import (not from-import) avoids a cycle when agents_blueprint is imported first
from orchestrator.agent_outputs import (
    DeepResearcherComplete,
    DeepResearcherNeedsMore,
    parse_deep_researcher_json,
)
from orchestrator.parse import (
    AgentOutputParseError,
    agent_error_reason,
    coerce_deep_researcher_state_machine,
    normalize_structured_output,
    _role_keys,
    parse_agent_json_or_yaml,
    preprocess_agent_text,
    run_agent_with_format_retries,
)
from orchestrator.phases.common import log
from orchestrator.research import parse_deep_researcher
from orchestrator.runner import AgentRunner
from orchestrator.schema_validation import AgentSchemaError
from orchestrator.state import flag_inactive


def run_structured_agent(
    runner: AgentRunner,
    role: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Spawn a JSON/YAML agent. Returns ``(parsed, error_reason)``."""
    try:
        raw = runner(role, payload)
    except AgentSchemaError as exc:
        return None, f"{role} output schema mismatch: {exc.cause}"
    try:
        parsed = parse_agent_json_or_yaml(raw, expected_keys=_role_keys(role))
    except AgentOutputParseError as exc:
        return None, f"{role} parse error: {exc}"
    spec = agents_blueprint.AGENTS[role]
    parsed = normalize_structured_output(
        role,
        payload,
        parsed,
        output_schema=spec.get("output_schema"),
    )
    err = agent_error_reason(parsed)
    if err:
        return parsed, f"{role} error: {err}"
    return parsed, None


def run_structured_or_flag(
    vault: ObsidianManager,
    market_id: str,
    phase: str,
    runner: AgentRunner,
    role: str,
    payload: dict[str, Any],
    flag_payload: Callable[[str], dict[str, Any]],
) -> dict[str, Any] | None:
    """``run_structured_agent`` that flags the market inactive on any error and returns ``None``."""
    parsed, err = run_structured_agent(runner, role, payload)
    if err:
        flag_inactive(vault, market_id, phase, err, flag_payload(err))
        return None
    return parsed


def invoke_deep_researcher(
    runner: AgentRunner,
    dr_in: dict[str, Any],
) -> DeepResearcherComplete | DeepResearcherNeedsMore:
    payload = dict(dr_in)

    def _parse_dr_response(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            base: Any = raw
        elif isinstance(raw, str):
            try:
                base = parse_agent_json_or_yaml(preprocess_agent_text(raw))
            except AgentOutputParseError:
                base = raw
        else:
            base = raw
        return coerce_deep_researcher_state_machine(base, payload)

    def _validate_dr(parsed: dict[str, Any]) -> DeepResearcherComplete | DeepResearcherNeedsMore:
        normalized = normalize_structured_output(
            "deep_researcher",
            payload,
            parsed,
            output_schema=None,
        )
        return parse_deep_researcher_json(normalized)

    return run_agent_with_format_retries(
        runner,
        "deep_researcher",
        payload,
        validate_fn=_validate_dr,
        parse_response=_parse_dr_response,
    )


def check_complete(
    vault: ObsidianManager,
    market_id: str,
    complete: DeepResearcherComplete,
    *,
    flag: bool = True,
    on_fail: Callable[[str, dict[str, Any]], None] | None = None,
) -> Any | None:
    """Cross-check JSON vs markdown frontmatter. Returns the parsed research or ``None``.

    ``flag=False`` (extra samples) only logs: one bad sample must not blacklist a market.
    ``on_fail`` (shadow runs) replaces the inactive flag: it is called with ``(reason, payload)``.
    """

    def fail(reason: str, payload: dict[str, Any]) -> None:
        if on_fail is not None:
            on_fail(reason, payload)
        elif flag:
            flag_inactive(vault, market_id, "phase3", reason, payload)
        else:
            log.warning("[PHASE 3] dropping sample for %s: %s", market_id, reason)

    try:
        research = parse_deep_researcher(complete.markdown)
    except (ValueError, AgentOutputParseError) as exc:
        fail(
            f"deep researcher markdown validation failed: {exc}",
            {"markdown": complete.markdown[:500]},
        )
        return None

    if research.error:
        fail(
            f"deep researcher error in frontmatter: {research.error}",
            research.frontmatter,
        )
        return None

    if complete.market_id != market_id:
        fail(
            f"deep researcher returned mismatched market_id: {complete.market_id!r}",
            {"expected": market_id, "got": complete.market_id},
        )
        return None

    if abs(complete.estimated_p - research.estimated_p) > 1e-6:
        fail(
            "deep researcher estimated_p mismatch between JSON and markdown frontmatter",
            {
                "json_estimated_p": complete.estimated_p,
                "markdown_estimated_p": research.estimated_p,
            },
        )
        return None

    for name, json_val, md_val in (
        ("p_low", complete.p_low, research.p_low),
        ("p_high", complete.p_high, research.p_high),
    ):
        if md_val is not None and abs(json_val - md_val) > 1e-6:
            fail(
                f"deep researcher {name} mismatch between JSON and markdown frontmatter",
                {f"json_{name}": json_val, f"markdown_{name}": md_val},
            )
            return None

    if research.market_id is not None and research.market_id != market_id:
        fail(
            f"deep researcher markdown market_id mismatch: {research.market_id!r}",
            research.frontmatter,
        )
        return None

    return research
