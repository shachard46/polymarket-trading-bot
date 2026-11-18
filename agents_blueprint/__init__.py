"""Per-role agent definitions: ``agents_blueprint/<role>/`` (system prompt in AGENTS.md, schemas in agent.yaml)."""

from __future__ import annotations

from agents_blueprint.loader import load_agents_from_dir

AGENTS = load_agents_from_dir()

__all__ = ["AGENTS", "load_agents_from_dir"]
