"""Per-role provider + model routing from ``config/models.yaml`` (+ env overrides).

Precedence for a role: ``LLM_ROLE_<ROLE>=provider:model`` env, then the YAML ``roles`` entry, then
``LLM_DEFAULT=provider:model`` env, then the YAML ``default``. Unknown providers/roles and missing keys raise
:class:`LLMError`.

Optional per-role ``batch: true`` (default false) in the ``roles:`` entry of models.yaml sends latency-insensitive
single-shot calls (triage, the triage-shadow quick estimate) through the provider's Batch API (Anthropic
Message Batches, Gemini batchGenerateContent; ~50% cheaper, minutes of latency) via ``LLM.complete_many``.
Providers without a batch API (OpenRouter) ignore it. ``PMBOT_LLM_BATCH_POLL_SEC`` (30) and
``PMBOT_LLM_BATCH_MAX_WAIT_SEC`` (1200) bound the wait; on timeout the batch is cancelled and the
unfinished items run synchronously. The flag is read from the YAML role entry even when an env override
swaps the provider/model.

Optional per-role `temperature` (float) and `reasoning` (bool; false disables thinking, unset = provider
default) follow the same rule. OpenRouter sends `temperature` and `reasoning: {enabled: false}`; Gemini sends
`generationConfig.temperature` and `thinkingConfig.thinkingLevel` (lowest supported level); Anthropic sends
`temperature` only.
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
    batch: bool = False
    temperature: float | None = None  # optional sampling temperature (all providers)
    reasoning: bool | None = None  # False disables thinking (OpenRouter, Gemini); unset = provider default


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
        "batch": cfg.roles[role].batch if role in cfg.roles else False,
        "temperature": cfg.roles[role].temperature if role in cfg.roles else None,
        "reasoning": cfg.roles[role].reasoning if role in cfg.roles else None,
    })


def llm_for(role: str) -> LLM:
    """One cached :class:`LLM` instance per distinct role spec."""
    spec = spec_for(role)
    key = (role, spec.provider, spec.model, spec.max_tokens, spec.web_max_uses, spec.batch,
           spec.temperature, spec.reasoning)
    if key not in _instances:
        llm = PROVIDERS[spec.provider](
            model=spec.model, max_tokens=spec.max_tokens, web_max_uses=spec.web_max_uses
        )
        llm.role = role
        llm.batch_enabled = spec.batch
        llm.temperature = spec.temperature
        llm.reasoning = spec.reasoning
        _instances[key] = llm
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
