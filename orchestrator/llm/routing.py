"""Per-role provider + model routing from ``config/models.yaml`` (+ env overrides).

Precedence for a role: ``LLM_ROLE_<ROLE>=provider:model`` env, then the YAML ``roles`` entry, then
``LLM_DEFAULT=provider:model`` env, then the YAML ``default``. Unknown providers/roles and missing keys raise
:class:`LLMError`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from orchestrator.config import DEFAULT_DIRECT_MAX_TOKENS, DEFAULT_DIRECT_WEB_MAX_USES
from orchestrator.llm.base import LLM, LLMError
from orchestrator.llm.registry import PROVIDERS

MODELS_CONFIG_ENV = "LLM_MODELS_CONFIG"
ROLE_ENV_PREFIX = "LLM_ROLE_"
DEFAULT_ENV = "LLM_DEFAULT"
DEFAULT_MODELS_PATH = Path(__file__).resolve().parents[2] / "config" / "models.yaml"


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    max_tokens: int | None = None
    web_max_uses: int | None = None


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tokens: int = DEFAULT_DIRECT_MAX_TOKENS
    web_max_uses: int = DEFAULT_DIRECT_WEB_MAX_USES
    default: ModelSpec
    roles: dict[str, ModelSpec] = {}


_instances: dict[tuple, LLM] = {}


def _known_roles() -> dict[str, dict[str, Any]]:
    import agents_blueprint  # noqa: PLC0415 - avoid import cycle at module load

    return agents_blueprint.AGENTS


def _check_provider(spec: ModelSpec, where: str) -> None:
    if spec.provider not in PROVIDERS:
        raise LLMError(f"{where}: unknown provider {spec.provider!r}; use one of {sorted(PROVIDERS)}")


def _parse_env_spec(name: str, raw: str) -> ModelSpec:
    provider, sep, model = raw.partition(":")
    provider, model = provider.strip().lower(), model.strip()
    if not sep or not provider or not model:
        raise LLMError(f"{name}={raw!r} must look like provider:model")
    spec = ModelSpec(provider=provider, model=model)
    _check_provider(spec, name)
    return spec


def load_config() -> ModelsConfig:
    path = Path(os.environ.get(MODELS_CONFIG_ENV, "").strip() or DEFAULT_MODELS_PATH)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LLMError(f"cannot read models config {path}: {exc}") from exc
    try:
        cfg = ModelsConfig.model_validate(data or {})
    except ValidationError as exc:
        raise LLMError(f"invalid models config {path}: {exc}") from exc
    roles = _known_roles()
    _check_provider(cfg.default, f"{path} default")
    for role, spec in cfg.roles.items():
        if role not in roles:
            raise LLMError(f"{path}: unknown role {role!r}; known roles: {sorted(roles)}")
        _check_provider(spec, f"{path} role {role}")
    return cfg


def spec_for(role: str) -> ModelSpec:
    """Resolved provider/model spec for ``role`` (also used for ledger fields)."""
    if role not in _known_roles():
        raise LLMError(f"unknown role {role!r}; known roles: {sorted(_known_roles())}")
    cfg = load_config()
    env_name = ROLE_ENV_PREFIX + role.upper()
    raw = os.environ.get(env_name, "").strip()
    if raw:
        spec = _parse_env_spec(env_name, raw)
    elif role in cfg.roles:
        spec = cfg.roles[role]
    else:
        raw = os.environ.get(DEFAULT_ENV, "").strip()
        spec = _parse_env_spec(DEFAULT_ENV, raw) if raw else cfg.default
    return spec.model_copy(update={
        "max_tokens": spec.max_tokens or cfg.max_tokens,
        "web_max_uses": spec.web_max_uses or cfg.web_max_uses,
    })


def llm_for(role: str) -> LLM:
    """One cached :class:`LLM` instance per distinct role spec."""
    spec = spec_for(role)
    key = (role, spec.provider, spec.model, spec.max_tokens, spec.web_max_uses)
    if key not in _instances:
        _instances[key] = PROVIDERS[spec.provider](
            model=spec.model, max_tokens=spec.max_tokens, web_max_uses=spec.web_max_uses
        )
    return _instances[key]


def validate() -> None:
    """Fail fast: every Hub-spawned role resolves to a spec and each provider in use has its key."""
    for name in os.environ:
        if name.startswith(ROLE_ENV_PREFIX) and name[len(ROLE_ENV_PREFIX):].lower() not in _known_roles():
            raise LLMError(f"{name}: unknown role; known roles: {sorted(_known_roles())}")
    used: dict[str, list[str]] = {}
    for role, agent in _known_roles().items():
        if not agent.get("hub_spawned", True):
            continue
        used.setdefault(spec_for(role).provider, []).append(role)
    for provider, roles in sorted(used.items()):
        try:
            PROVIDERS[provider].check_key()
        except LLMError as exc:
            raise LLMError(f"{exc} (used by role(s) {', '.join(sorted(roles))})") from exc
