"""Per-call LLM token usage and cost: one JSONL row per call in ``Vault/05_Ledger/llm_usage.jsonl``.

Prices come from ``config/prices.yaml`` (user-maintained); an unknown model gets ``cost_usd: null``.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import yaml

from config.vault import resolve_vault_base
from orchestrator.llm.base import LLMResponse

log = logging.getLogger(__name__)

PRICES_PATH = Path(__file__).resolve().parents[2] / "config" / "prices.yaml"
USAGE_FILE = "llm_usage.jsonl"
DEFAULT_BATCH_DISCOUNT = 0.5

_market_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("llm_usage_market_id", default=None)


@contextlib.contextmanager
def market_scope(market_id: str | None) -> Iterator[None]:
    """Attribute every :func:`record` call inside the block to ``market_id``."""
    token = _market_id.set(market_id)
    try:
        yield
    finally:
        _market_id.reset(token)


def current_market() -> str | None:
    return _market_id.get()


def usage_path(vault_base: str | Path | None = None) -> Path:
    return resolve_vault_base(vault_base) / "Vault" / "05_Ledger" / USAGE_FILE


def load_prices(path: Path | None = None) -> dict[str, Any]:
    """Parsed prices.yaml (``{}`` if missing or invalid)."""
    try:
        data = yaml.safe_load((path or PRICES_PATH).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def compute_cost(
    provider: str, model: str, input_tokens: int | None, output_tokens: int | None,
    *, batch: bool = False, prices: dict[str, Any] | None = None,
) -> float | None:
    """USD cost, or ``None`` when the model has no price or token counts are missing."""
    prices = load_prices() if prices is None else prices
    entry = ((prices.get("providers") or {}).get(provider) or {}).get(model)
    if not isinstance(entry, dict):
        return None
    try:
        p_in, p_out = float(entry["input"]), float(entry["output"])
    except (KeyError, TypeError, ValueError):
        return None
    if input_tokens is None and output_tokens is None:
        return None
    cost = ((input_tokens or 0) * p_in + (output_tokens or 0) * p_out) / 1_000_000
    if batch:
        try:
            cost *= float(prices.get("batch_discount", DEFAULT_BATCH_DISCOUNT))
        except (TypeError, ValueError):
            cost *= DEFAULT_BATCH_DISCOUNT
    return cost


def record(role: str, resp: LLMResponse, *, batch: bool = False) -> None:
    """Append one usage row; never raises."""
    try:
        row = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "role": role,
            "provider": resp.provider,
            "model": resp.model,
            "market_id": current_market(),
            "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens,
            "batch": bool(batch),
            "cost_usd": compute_cost(resp.provider, resp.model, resp.input_tokens, resp.output_tokens, batch=batch),
        }
        path = usage_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:  # noqa: BLE001 - accounting must never break a run
        log.warning("llm usage record failed", exc_info=True)


def iter_usage(vault_base: str | Path | None = None) -> list[dict[str, Any]]:
    """All usage rows (bad lines skipped)."""
    path = usage_path(vault_base)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out
