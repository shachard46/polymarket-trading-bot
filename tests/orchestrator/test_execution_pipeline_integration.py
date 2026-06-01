"""Live Phase 4 smoke: pre-researched market → Executioner → trade log JSON.

Opt-in only (skipped in normal ``pytest`` runs). Use on a host with:

- OpenClaw Gateway
- live ``polymarket-executioner`` agent with ``calculate_trade_allocation``
- ``PAPER_TRADE_MODE=True`` in ``config/trading_constants.py`` (default)
- a vault that already has Phase 3 research for your market
  (``02_Active_Research/{market_id}.md``)
- a readable polymarket-scraper DB (``POLYMARKET_DB_PATH``)
- ``poly-scan`` on PATH (or ``POLY_SCAN_BIN``)

This runs Phase 4 in isolation (no phases 1–3, 5–6). You pass a **pre-researched**
``market_id``; the test reads ``p_value`` from vault active research, hydrates
``market_data`` from the scraper DB, and calls ``phase4_execution`` with a
single-market list.

Trade output is JSON at ``Vault/03_Trades/{market_id}.json`` (not markdown).

```bash
export OPENCLAW_PHASE4_LIVE=1
export OPENCLAW_ORCHESTRATOR_MODE=live
export OPENCLAW_PHASE4_MARKET_ID=0x...          # required: pre-researched market
export OPENCLAW_VAULT_PATH=/path/to/vault       # required: must contain active research
export POLYMARKET_DB_PATH=/path/to/polymarket.db

python tests/orchestrator/test_execution_pipeline_integration.py

# pytest with live logs:
pytest tests/orchestrator/test_execution_pipeline_integration.py -v -s --log-cli-level=INFO
# optional: export OPENCLAW_PHASE4_LOG_LEVEL=DEBUG
```
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import pytest

from agents_blueprint import AGENTS
from config.vault import VAULT_PATH_ENV
from obsidian_utils import ObsidianManager, TradeLogPayload
from orchestrator import phases, scraper
from orchestrator.config import PAPER_TRADE_MODE, RUNNER_MODE_ENV, RUNNER_MODE_LIVE
from orchestrator.openclaw_cli import require_gateway
from orchestrator.research import parse_deep_researcher
from orchestrator.runner import spawn_agent
from orchestrator.schema_validation import build_model, validate_payload
from orchestrator.state import is_inactive

_PHASE4_LIVE_ENV = "OPENCLAW_PHASE4_LIVE"
_PHASE4_MARKET_ENV = "OPENCLAW_PHASE4_MARKET_ID"
_LOG_LEVEL_ENV = "OPENCLAW_PHASE4_LOG_LEVEL"

log = logging.getLogger(__name__)


def _configure_logging() -> None:
    level_name = os.environ.get(_LOG_LEVEL_ENV, "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )


def _log_status(step: str, **details: object) -> None:
    if details:
        detail_str = " ".join(f"{key}={value!r}" for key, value in details.items())
        log.info("[phase4_live] %s — %s", step, detail_str)
    else:
        log.info("[phase4_live] %s", step)


def _phase4_live_requested() -> bool:
    return os.environ.get(_PHASE4_LIVE_ENV) == "1"


def _require_market_id() -> str:
    market_id = os.environ.get(_PHASE4_MARKET_ENV, "").strip()
    if not market_id:
        raise RuntimeError(
            f"{_PHASE4_MARKET_ENV} is required for this live test "
            "(pre-researched market_id)."
        )
    return market_id


def _require_vault_path() -> Path:
    explicit = os.environ.get(VAULT_PATH_ENV, "").strip()
    if not explicit:
        raise RuntimeError(
            f"{VAULT_PATH_ENV} is required for this live test "
            "(vault must already contain 02_Active_Research for your market)."
        )
    return Path(explicit).expanduser().resolve()


def _wire_scraper_env(monkeypatch: pytest.MonkeyPatch | None = None) -> None:
    db_path = os.environ.get("POLYMARKET_DB_PATH", "").strip()
    if not db_path:
        raise RuntimeError("POLYMARKET_DB_PATH is required for this live test.")

    if monkeypatch is not None:
        monkeypatch.setenv("POLYMARKET_DB_PATH", db_path)
    else:
        os.environ["POLYMARKET_DB_PATH"] = db_path


def _make_vault(*, base: Path) -> ObsidianManager:
    mgr = ObsidianManager(vault_base=base)
    mgr.cold_start_protocol()
    return mgr


def _build_researched_row(vault: ObsidianManager, market_id: str) -> dict[str, Any]:
    """Build the Phase 4 input row from vault research and scraper market_data."""
    active = vault.read_active_research(market_id)
    if active is None:
        raise RuntimeError(
            f"No active research at 02_Active_Research/{market_id}.md in vault "
            f"{vault._base!s}. Run Phase 3 first or fix {VAULT_PATH_ENV}."
        )

    research = parse_deep_researcher(active)
    if not (0.0 <= research.estimated_p <= 1.0):
        raise RuntimeError(
            f"estimated_p out of range for {market_id}: {research.estimated_p!r}"
        )

    row = scraper.fetch_market_row(market_id)
    if row is None:
        raise RuntimeError(
            f"scraper.fetch_market_row returned None for market_id={market_id!r}. "
            "Check POLYMARKET_DB_PATH and poly-scan."
        )

    return {
        "market_id": market_id,
        "p_value": research.estimated_p,
        "market_data": row.market_data or {},
    }


def _run_live_execution(vault: ObsidianManager, row: dict[str, Any]) -> None:
    """Execute single-market Phase 4 and log progress."""
    market_id = row["market_id"]
    _log_status(
        "starting phase4_execution",
        market_id=market_id,
        p_value=row["p_value"],
        runner_mode=os.environ.get(RUNNER_MODE_ENV, RUNNER_MODE_LIVE),
        paper_trade_mode=PAPER_TRADE_MODE,
    )
    phases.phase4_execution(vault, [row], runner=spawn_agent)
    _log_status("phase4_execution complete", market_id=market_id)


def _assert_trade_log_ok(vault: ObsidianManager, market_id: str) -> dict[str, Any]:
    """Validate trade JSON, executioner contract, and paper-trade invariants."""
    trade = vault.read_trade_log_dict(market_id)
    assert trade is not None, (
        f"03_Trades/{market_id}.json missing after phase4_execution; "
        f"check vault inactive flags on trades/active artifacts"
    )
    assert not is_inactive(trade), (
        f"trade log marked inactive: {trade.get('error_log')!r}"
    )

    validated = TradeLogPayload.model_validate(trade)
    assert validated.market_id == market_id

    output_model = build_model(
        "executioner_output",
        AGENTS["executioner"]["output_schema"],
    )
    assert output_model is not None
    validate_payload("executioner", "output", output_model, trade)

    assert trade["executed"] is False, "paper trade must not set executed=True"
    assert trade["transaction_hash"] is None, (
        "paper trade must not persist a transaction_hash"
    )

    allocation = trade["allocation_usd"]
    assert isinstance(allocation, (int, float))
    assert float(allocation) >= 0.0

    return trade


def run_phase4_isolated() -> int:
    """Run Phase 4 once with live executioner; return process exit code."""
    _configure_logging()
    os.environ.setdefault(_PHASE4_LIVE_ENV, "1")
    os.environ.setdefault(RUNNER_MODE_ENV, RUNNER_MODE_LIVE)

    _log_status(
        "run started",
        live_env=_PHASE4_LIVE_ENV,
        runner_mode=os.environ.get(RUNNER_MODE_ENV),
        paper_trade_mode=PAPER_TRADE_MODE,
    )

    if not PAPER_TRADE_MODE:
        log.error("PAPER_TRADE_MODE must be True for this smoke test.")
        return 2

    if not _phase4_live_requested():
        log.error("Set %s=1 to run.", _PHASE4_LIVE_ENV)
        return 2

    try:
        _log_status("checking OpenClaw gateway")
        require_gateway()
        _log_status("gateway ok")
    except Exception as exc:
        log.error("OpenClaw gateway not ready: %s", exc)
        return 2

    try:
        market_id = _require_market_id()
        vault_base = _require_vault_path()
        _log_status(
            "wiring scraper env",
            db_path=os.environ.get("POLYMARKET_DB_PATH", ""),
            market_id=market_id,
            vault=str(vault_base),
        )
        _wire_scraper_env()
    except Exception as exc:
        log.error("Setup failed: %s", exc)
        return 2

    _log_status("initializing vault", vault=str(vault_base))
    vault = _make_vault(base=vault_base)

    try:
        row = _build_researched_row(vault, market_id)
    except Exception as exc:
        log.error("Failed to build Phase 4 row: %s", exc)
        return 2

    _log_status(
        "phase4 row ready",
        market_id=row["market_id"],
        p_value=row["p_value"],
        market_data_keys=sorted(row["market_data"].keys()),
    )

    _run_live_execution(vault, row)

    try:
        trade = _assert_trade_log_ok(vault, market_id)
    except AssertionError as exc:
        log.error("Trade log validation failed: %s", exc)
        return 1

    _log_status(
        "run finished OK",
        market_id=market_id,
        allocation_usd=trade.get("allocation_usd"),
        below_edge_threshold=trade.get("below_edge_threshold"),
    )
    return 0


@pytest.fixture()
def phase4_live_env(monkeypatch):
    _configure_logging()
    if not _phase4_live_requested():
        _log_status("skipped", reason=f"{_PHASE4_LIVE_ENV} not set")
        pytest.skip(
            f"Set {_PHASE4_LIVE_ENV}=1 for live Phase 4. "
            "Or: python tests/orchestrator/test_execution_pipeline_integration.py"
        )

    if not PAPER_TRADE_MODE:
        pytest.skip("PAPER_TRADE_MODE must be True for this live smoke test.")

    _log_status("pytest live env: checking prerequisites")
    monkeypatch.setenv(RUNNER_MODE_ENV, RUNNER_MODE_LIVE)
    try:
        require_gateway()
        _log_status("pytest live env: gateway ok")
    except Exception as exc:
        _log_status("pytest live env: skipped", reason=f"gateway: {exc}")
        pytest.skip(f"OpenClaw gateway not ready: {exc}")

    try:
        _require_market_id()
        _require_vault_path()
        _wire_scraper_env(monkeypatch)
        market_id = _require_market_id()
        row = scraper.fetch_market_row(market_id)
        if row is None:
            pytest.skip(
                f"scraper.fetch_market_row returned None for {market_id!r} "
                "(check POLYMARKET_DB_PATH and poly-scan)."
            )
        _log_status("pytest live env: scraper ok", market_id=market_id)
    except Exception as exc:
        _log_status("pytest live env: skipped", reason=str(exc))
        pytest.skip(str(exc))


@pytest.fixture()
def phase4_vault():
    explicit = os.environ.get(VAULT_PATH_ENV, "").strip()
    if not explicit:
        pytest.skip(
            f"{VAULT_PATH_ENV} is required for Phase 4 live test "
            "(vault must contain pre-researched 02_Active_Research)."
        )
    base = Path(explicit).expanduser().resolve()
    return _make_vault(base=base)


def test_phase4_live_one_pre_researched_market(phase4_live_env, phase4_vault):
    """Live Phase 4 on one pre-researched market (vault + DB, paper trade)."""
    assert PAPER_TRADE_MODE is True

    _log_status("pytest test started")
    market_id = _require_market_id()
    vault = phase4_vault

    row = _build_researched_row(vault, market_id)
    assert row["market_id"] == market_id
    assert 0.0 <= row["p_value"] <= 1.0

    _run_live_execution(vault, row)

    _log_status("validating trade log", market_id=market_id)
    trade = _assert_trade_log_ok(vault, market_id)
    _log_status(
        "pytest test passed",
        market_id=market_id,
        allocation_usd=trade.get("allocation_usd"),
        below_edge_threshold=trade.get("below_edge_threshold"),
    )


if __name__ == "__main__":
    raise SystemExit(run_phase4_isolated())
