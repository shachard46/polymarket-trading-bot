"""Live Phase 3 smoke: one DB market → Briefer → A-IQ → Deep Researcher.

Opt-in only (skipped in normal ``pytest`` runs). Use on a host with:
- OpenClaw Gateway
- live briefer/deep_researcher agents
- working A-IQ
- a readable polymarket-scraper DB (configured via ``POLYMARKET_DB_PATH``)
- ``poly-scan`` on PATH (or ``POLY_SCAN_BIN``)

This runs Phase 3 in isolation (no phases 1–2, 4–6). It fetches one market through
``orchestrator.scraper`` and researches **only that market** via
``phase3_research_market`` (no full ``01_Filters/`` queue scan).

```bash
export OPENCLAW_PHASE3_LIVE=1
export OPENCLAW_ORCHESTRATOR_MODE=live
export OPENCLAW_VAULT_PATH=/tmp/phase3-live-vault
export POLYMARKET_DB_PATH=/path/to/polymarket.db
# optional: pin a market_id instead of ingesting the freshest open row
# export OPENCLAW_PHASE3_MARKET_ID=0x...

python tests/orchestrator/test_research_pipeline_integration.py

# pytest with live logs:
pytest tests/orchestrator/test_research_pipeline_integration.py -v -s --log-cli-level=INFO
# optional: export OPENCLAW_PHASE3_LOG_LEVEL=DEBUG
```
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

from config.vault import VAULT_PATH_ENV, resolve_vault_base
from orchestrator import phases, scraper
from orchestrator.config import RUNNER_MODE_ENV, RUNNER_MODE_LIVE
from orchestrator.openclaw_cli import require_gateway
from orchestrator.research import parse_deep_researcher
from orchestrator.runner import spawn_agent
from orchestrator.scraper import MarketRow

_PHASE3_LIVE_ENV = "OPENCLAW_PHASE3_LIVE"
_PHASE3_MARKET_ENV = "OPENCLAW_PHASE3_MARKET_ID"
_LOG_LEVEL_ENV = "OPENCLAW_PHASE3_LOG_LEVEL"

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
        log.info("[phase3_live] %s — %s", step, detail_str)
    else:
        log.info("[phase3_live] %s", step)


def _phase3_live_requested() -> bool:
    return os.environ.get(_PHASE3_LIVE_ENV) == "1"

def _wire_scraper_env(monkeypatch: pytest.MonkeyPatch | None = None) -> None:
    db_path = os.environ.get("POLYMARKET_DB_PATH", "").strip()
    if not db_path:
        raise RuntimeError("POLYMARKET_DB_PATH is required for this live test.")

    if monkeypatch is not None:
        monkeypatch.setenv("POLYMARKET_DB_PATH", db_path)
        monkeypatch.setenv("OPENCLAW_INGEST_LIMIT", "1")
    else:
        os.environ["POLYMARKET_DB_PATH"] = db_path
        os.environ["OPENCLAW_INGEST_LIMIT"] = "1"


def _resolve_market_row() -> MarketRow:
    _wire_scraper_env()
    pinned = os.environ.get(_PHASE3_MARKET_ENV, "").strip()
    if pinned:
        _log_status("fetching pinned market", market_id=pinned)
        row = scraper.fetch_market_row(pinned)
        if row is None:
            raise RuntimeError(f"scraper.fetch_market_row returned None for market_id={pinned!r}")
        _log_status("market resolved (pinned)", market_id=row.market_id, title=row.market_title)
        return row

    _log_status("fetching one open market", ingest_limit=os.environ.get("OPENCLAW_INGEST_LIMIT", "1"))
    rows = scraper.fetch_target_markets()
    if not rows:
        raise RuntimeError(
            "scraper.fetch_target_markets returned no rows. "
            "Check POLYMARKET_DB_PATH and that poly-scan is on PATH (or POLY_SCAN_BIN)."
        )
    row = rows[0]
    _log_status("market resolved (ingest)", market_id=row.market_id, title=row.market_title)
    return row


def _make_vault(*, base: Path | None = None):
    from obsidian_utils import ObsidianManager

    if base is None:
        base = resolve_vault_base()
    mgr = ObsidianManager(vault_base=base)
    mgr.cold_start_protocol()
    return mgr


def _seed_filter_for_phase3(vault, market: MarketRow) -> None:
    _log_status("seeding filter log", market_id=market.market_id, vault=str(vault._base))
    vault.write_filter_log(
        market.market_id,
        {
            "market_id": market.market_id,
            "passed": True,
            "trigger": "phase3_live_smoke",
            "confidence_multiplier": 1.0,
            "details": "Seeded for isolated Phase 3 live run (skips phases 1–2).",
            "error": None,
        },
    )


def _run_live_research(vault, market: MarketRow):
    """Execute single-market Phase 3 and log progress."""
    _log_status(
        "hydrating market row",
        market_id=market.market_id,
        runner_mode=os.environ.get(RUNNER_MODE_ENV, RUNNER_MODE_LIVE),
    )
    hydrated = scraper.fetch_market_row(market.market_id)
    if hydrated is None:
        _log_status("hydration failed", market_id=market.market_id)
        return None
    _log_status("hydration ok", market_id=hydrated.market_id, title=hydrated.market_title)

    _log_status("starting phase3_research_market", market_id=market.market_id)
    result = phases.phase3_research_market(vault, market.market_id, runner=spawn_agent)
    if result is None:
        filt = vault.read_filter_log(market.market_id)
        _log_status("phase3_research_market failed", market_id=market.market_id, filter=filt)
        return None

    bundle = vault.read_research_bundle(market.market_id)
    active = vault.read_active_research(market.market_id)
    _log_status(
        "phase3_research_market complete",
        market_id=result.get("market_id"),
        p_value=result.get("p_value"),
        bundle_queries=len(bundle or []),
        active_research=bool(active),
    )
    return result


def run_phase3_isolated() -> int:
    """Run Phase 3 once with live agents and real A-IQ; return process exit code."""
    _configure_logging()
    os.environ.setdefault(_PHASE3_LIVE_ENV, "1")
    os.environ.setdefault(RUNNER_MODE_ENV, RUNNER_MODE_LIVE)

    _log_status("run started", live_env=_PHASE3_LIVE_ENV, runner_mode=os.environ.get(RUNNER_MODE_ENV))

    if not _phase3_live_requested():
        log.error("Set %s=1 to run.", _PHASE3_LIVE_ENV)
        return 2

    try:
        _log_status("checking OpenClaw gateway")
        require_gateway()
        _log_status("gateway ok")
    except Exception as exc:
        log.error("OpenClaw gateway not ready: %s", exc)
        return 2

    try:
        _log_status("wiring scraper env", db_path=os.environ.get("POLYMARKET_DB_PATH", ""))
        _wire_scraper_env()
        market = _resolve_market_row()
    except Exception as exc:
        log.error("Setup failed: %s", exc)
        return 2

    if not os.environ.get(VAULT_PATH_ENV, "").strip():
        log.warning("Hint: set %s to keep vault artifacts after this run.", VAULT_PATH_ENV)

    _log_status("initializing vault")
    vault = _make_vault()
    _seed_filter_for_phase3(vault, market)

    result = _run_live_research(vault, market)
    if result is None:
        return 1

    active = vault.read_active_research(market.market_id)
    if active:
        try:
            research = parse_deep_researcher(active)
            _log_status("active research parsed", estimated_p=research.estimated_p)
        except ValueError as exc:
            log.warning("active research parse warning: %s", exc)

    _log_status("run finished OK", market_id=market.market_id, p_value=result.get("p_value"))
    return 0


@pytest.fixture()
def phase3_live_env(monkeypatch):
    _configure_logging()
    if not _phase3_live_requested():
        _log_status("skipped", reason=f"{_PHASE3_LIVE_ENV} not set")
        pytest.skip(
            f"Set {_PHASE3_LIVE_ENV}=1 for live Phase 3. "
            "Or: python tests/orchestrator/test_research_pipeline_integration.py"
        )

    _log_status("pytest live env: checking prerequisites")
    monkeypatch.setenv(RUNNER_MODE_ENV, RUNNER_MODE_LIVE)
    try:
        require_gateway()
        _log_status("pytest live env: gateway ok")
    except Exception as exc:
        _log_status("pytest live env: skipped", reason=f"gateway: {exc}")
        pytest.skip(f"OpenClaw gateway not ready: {exc}")

    try:
        _wire_scraper_env(monkeypatch)
        if not scraper.fetch_target_markets():
            _log_status(
                "pytest live env: skipped",
                reason="fetch_target_markets returned no rows",
            )
            pytest.skip(
                "scraper.fetch_target_markets returned no rows (check POLYMARKET_DB_PATH and poly-scan)."
            )
        _log_status("pytest live env: scraper ok")
    except Exception as exc:
        _log_status("pytest live env: skipped", reason=str(exc))
        pytest.skip(str(exc))


@pytest.fixture()
def phase3_vault(tmp_path):
    explicit = os.environ.get(VAULT_PATH_ENV, "").strip()
    base = Path(explicit).expanduser().resolve() if explicit else tmp_path
    return _make_vault(base=base)


def test_phase3_live_one_market_from_db(phase3_live_env, phase3_vault):
    """Live Phase 3 on one fetched market only (no vault queue scan)."""
    _log_status("pytest test started")
    market = _resolve_market_row()
    vault = phase3_vault
    _seed_filter_for_phase3(vault, market)

    result = _run_live_research(vault, market)
    assert result is not None, (
        f"phase3_research_market failed; filter={vault.read_filter_log(market.market_id)!r}"
    )
    assert result["market_id"] == market.market_id
    assert 0.0 <= result["p_value"] <= 1.0

    _log_status("validating vault artifacts", market_id=market.market_id)
    active = vault.read_active_research(market.market_id)
    assert active is not None
    research = parse_deep_researcher(active)
    assert research.market_id in (None, market.market_id)
    assert "## Bull Thesis" in research.body

    bundle = vault.read_research_bundle(market.market_id)
    assert bundle is not None
    assert len(bundle) >= 1
    assert any(
        (entry.get("research_data") or "").strip() or entry.get("error")
        for entry in bundle
    )
    _log_status("pytest test passed", market_id=market.market_id, p_value=result["p_value"])


if __name__ == "__main__":
    raise SystemExit(run_phase3_isolated())
