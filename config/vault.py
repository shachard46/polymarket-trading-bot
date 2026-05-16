"""Vault workspace path resolution (env + defaults)."""

from __future__ import annotations

import os
from pathlib import Path

VAULT_PATH_ENV = "OPENCLAW_VAULT_PATH"

_DEFAULT_VAULT_BASE = Path(__file__).resolve().parent.parent


def resolve_vault_base(explicit: str | Path | None = None) -> Path:
    """Resolve vault workspace root: explicit arg, then env, then project default."""
    if explicit is not None:
        return Path(explicit).resolve()
    from_env = os.environ.get(VAULT_PATH_ENV)
    if from_env:
        return Path(from_env).resolve()
    return _DEFAULT_VAULT_BASE
