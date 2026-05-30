"""Per-role OpenClaw-style agent workspaces (see ``agents/<role>/``)."""

from __future__ import annotations

from agents.loader import load_agents_from_dir

AGENTS = load_agents_from_dir()

__all__ = ["AGENTS", "load_agents_from_dir"]
