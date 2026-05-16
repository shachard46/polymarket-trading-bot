"""Discover per-role agent workspaces under ``agents/<role>/`` and build ``AGENTS``.

Layout follows OpenClaw workspace conventions: each subdirectory is a mini-workspace
with ``AGENTS.md`` (operating instructions / system prompt) and ``agent.yaml``
(metadata, schemas, tool allowlist). See https://docs.openclaw.ai/concepts/agent-workspace
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from orchestrator.schema_validation import build_model, is_markdown_output

_AGENTS_MD = "AGENTS.md"
_AGENT_YAML = "agent.yaml"
_SKIP_DIR_PREFIXES = ("_", ".")


def load_agents_from_dir(agents_root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load all agent workspaces; return orchestrator ``AGENTS`` dict keyed by role."""
    root = (agents_root or Path(__file__).resolve().parent).resolve()
    agents: dict[str, dict[str, Any]] = {}

    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(_SKIP_DIR_PREFIXES) or child.name == "__pycache__":
            continue
        try:
            yaml_path = child / _AGENT_YAML
            md_path = child / _AGENTS_MD
            if not yaml_path.is_file():
                raise FileNotFoundError(f"missing {yaml_path}")
            if not md_path.is_file():
                raise FileNotFoundError(f"missing {md_path}")

            meta = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                raise ValueError(f"{yaml_path} must contain a YAML mapping")

            role = str(meta.get("id") or child.name)
            if role != child.name:
                raise ValueError(
                    f"{yaml_path}: id {role!r} must match directory name {child.name!r}"
                )

            system_prompt = md_path.read_text(encoding="utf-8").strip()
            in_schema: Any = meta.get("input_schema") or {}
            out_schema: Any = meta.get("output_schema", {})

            agents[role] = {
                "system_prompt": system_prompt,
                "input_schema": in_schema,
                "output_schema": out_schema,
                # Pre-built validators applied by ``orchestrator.runner.spawn_agent``.
                "input_model": build_model(f"{role.title()}Input", in_schema),
                "output_model": build_model(f"{role.title()}Output", out_schema),
                "output_is_markdown": is_markdown_output(out_schema),
                "workspace_path": str(child.resolve()),
                "openclaw": {
                    "openclaw_agent_id": meta.get("openclaw_agent_id"),
                    "description": meta.get("description"),
                    "model": meta.get("model"),
                    "tools": meta.get("tools"),
                },
            }
        except FileNotFoundError as e:
            raise FileNotFoundError(f"missing {yaml_path}") from e
        except ValueError as e:
            raise ValueError(f"{yaml_path} must contain a YAML mapping") from e
        except Exception as e:
            raise Exception(f"error loading {yaml_path}") from e
    return agents
