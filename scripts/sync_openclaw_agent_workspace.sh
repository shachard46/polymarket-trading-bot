#!/usr/bin/env bash
# Sync agents_blueprint/<role>/ into an OpenClaw agent workspace directory.
#
# Usage:
#   OPENCLAW_AGENTS_ROOT=~/.openclaw/agents ./scripts/sync_openclaw_agent_workspace.sh overseer
#   OPENCLAW_AGENTS_ROOT=~/.openclaw/agents ./scripts/sync_openclaw_agent_workspace.sh all
#
# Copies AGENTS.md, agent.yaml, and TOOLS.md (when present) into
#   $OPENCLAW_AGENTS_ROOT/<openclaw_agent_id>/
# using openclaw_agent_id from each role's agent.yaml.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BLUEPRINT="${ROOT}/agents_blueprint"
TARGET_ROOT="${OPENCLAW_AGENTS_ROOT:-}"

if [[ -z "${TARGET_ROOT}" ]]; then
  echo "Set OPENCLAW_AGENTS_ROOT to your OpenClaw agents directory." >&2
  exit 1
fi

sync_role() {
  local role="$1"
  local src="${BLUEPRINT}/${role}"
  local yaml="${src}/agent.yaml"
  if [[ ! -f "${yaml}" ]]; then
    echo "skip ${role}: no agent.yaml" >&2
    return 0
  fi
  local agent_id
  agent_id="$(python3 -c "import yaml; print(yaml.safe_load(open('${yaml}'))['openclaw_agent_id'])")"
  local dest="${TARGET_ROOT}/${agent_id}"
  mkdir -p "${dest}"
  for f in AGENTS.md agent.yaml TOOLS.md; do
    if [[ -f "${src}/${f}" ]]; then
      cp "${src}/${f}" "${dest}/${f}"
      echo "synced ${role}/${f} -> ${dest}/${f}"
    fi
  done
}

ROLE="${1:-}"
if [[ -z "${ROLE}" ]]; then
  echo "usage: $0 <role|all>" >&2
  exit 1
fi

if [[ "${ROLE}" == "all" ]]; then
  for d in "${BLUEPRINT}"/*/; do
    sync_role "$(basename "${d}")"
  done
else
  sync_role "${ROLE}"
fi

echo "Done. Restart or reload the OpenClaw gateway if agents are cached."
