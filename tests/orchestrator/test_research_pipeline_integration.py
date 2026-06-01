"""Live Phase 3 smoke: one DB market → Briefer → A-IQ → Deep Researcher.

Opt-in only (skipped in normal ``pytest`` runs). Use on a host with:
- OpenClaw Gateway
- live briefer/deep_researcher agents
- working A-IQ
- a readable polymarket-scraper DB (configured via ``POLYMARKET_DB_PATH``)
- ``poly-scan`` on PATH (or ``POLY_SCAN_BIN``)

This runs Phase 3 in isolation (no phases 1–2, 4–6). It fetches markets exactly like the
Orchestrator does: through ``orchestrator.scraper``.

```bash
export OPENCLAW_PHASE3_LIVE=1
export OPENCLAW_ORCHESTRATOR_MODE=live
export OPENCLAW_VAULT_PATH=/tmp/phase3-live-vault
export POLYMARKET_DB_PATH=/path/to/polymarket.db
# optional: pin a market_id instead of ingesting the freshest open row
# export OPENCLAW_PHASE3_MARKET_ID=0x...

python tests/orchestrator/test_research_pipeline_integration.py
```
"""

from __future__ import annotations

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
        row = scraper.fetch_market_row(pinned)
        if row is None:
            raise RuntimeError(f"scraper.fetch_market_row returned None for market_id={pinned!r}")
        return row

    rows = scraper.fetch_target_markets()
    if not rows:
        raise RuntimeError(
            "scraper.fetch_target_markets returned no rows. "
            "Check POLYMARKET_DB_PATH and that poly-scan is on PATH (or POLY_SCAN_BIN)."
        )
    return rows[0]


def _make_vault(*, base: Path | None = None):
    from obsidian_utils import ObsidianManager

    if base is None:
        base = resolve_vault_base()
    mgr = ObsidianManager(vault_base=base)
    mgr.cold_start_protocol()
    return mgr


def _seed_filter_for_phase3(vault, market: MarketRow) -> None:
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


def run_phase3_isolated() -> int:
    """Run Phase 3 once with live agents and real A-IQ; return process exit code."""
    os.environ.setdefault(_PHASE3_LIVE_ENV, "1")
    os.environ.setdefault(RUNNER_MODE_ENV, RUNNER_MODE_LIVE)

    if not _phase3_live_requested():
        print(f"Set {_PHASE3_LIVE_ENV}=1 to run.", file=sys.stderr)
        return 2

    try:
        require_gateway()
    except Exception as exc:
        print(f"OpenClaw gateway not ready: {exc}", file=sys.stderr)
        return 2

    try:
        _wire_scraper_env()
        market = _resolve_market_row()
    except Exception as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 2

    if not os.environ.get(VAULT_PATH_ENV, "").strip():
        print(
            f"Hint: set {VAULT_PATH_ENV} to keep vault artifacts after this run.",
            file=sys.stderr,
        )

    vault = _make_vault()
    _seed_filter_for_phase3(vault, market)

    print(f"vault={vault._base}")
    print(f"db={os.environ.get('POLYMARKET_DB_PATH')}")
    print(f"market_id={market.market_id}")
    print(f"title={market.market_title!r}")

    out = phases.phase3_qualitative_pipeline(vault, runner=spawn_agent)

    if len(out) != 1:
        filt = vault.read_filter_log(market.market_id)
        print(f"phase3 returned {len(out)} markets; filter={filt!r}", file=sys.stderr)
        return 1

    row = out[0]
    active = vault.read_active_research(market.market_id)
    bundle = vault.read_research_bundle(market.market_id)
    print(f"p_value={row.get('p_value')}")
    print(f"bundle_queries={len(bundle or [])}")
    if active:
        try:
            research = parse_deep_researcher(active)
            print(f"estimated_p={research.estimated_p}")
        except ValueError as exc:
            print(f"active research parse warning: {exc}", file=sys.stderr)
    print("Phase 3 live run finished OK.")
    return 0


@pytest.fixture()
def phase3_live_env(monkeypatch):
    if not _phase3_live_requested():
        pytest.skip(
            f"Set {_PHASE3_LIVE_ENV}=1 for live Phase 3. "
            "Or: python tests/orchestrator/test_research_pipeline_integration.py"
        )

    monkeypatch.setenv(RUNNER_MODE_ENV, RUNNER_MODE_LIVE)
    try:
        require_gateway()
    except Exception as exc:
        pytest.skip(f"OpenClaw gateway not ready: {exc}")

    try:
        _wire_scraper_env(monkeypatch)
        if not scraper.fetch_target_markets():
            pytest.skip(
                "scraper.fetch_target_markets returned no rows (check POLYMARKET_DB_PATH and poly-scan)."
            )
    except Exception as exc:
        pytest.skip(str(exc))


@pytest.fixture()
def phase3_vault(tmp_path):
    explicit = os.environ.get(VAULT_PATH_ENV, "").strip()
    base = Path(explicit).expanduser().resolve() if explicit else tmp_path
    return _make_vault(base=base)


def test_phase3_live_one_market_from_db(phase3_live_env, phase3_vault):
    """Live Phase 3 on one market fetched via poly-scan (phases 1–2 skipped)."""
    market = _resolve_market_row()
    vault = phase3_vault
    _seed_filter_for_phase3(vault, market)

    hydrated = scraper.fetch_market_row(market.market_id)
    assert hydrated is not None
    assert hydrated.market_id == market.market_id
    assert hydrated.market_title == market.market_title

    out = phases.phase3_qualitative_pipeline(vault, runner=spawn_agent)
    assert len(out) == 1, (
        f"expected one researched market; filter={vault.read_filter_log(market.market_id)!r}"
    )
    assert out[0]["market_id"] == market.market_id
    assert 0.0 <= out[0]["p_value"] <= 1.0

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


if __name__ == "__main__":
    raise SystemExit(run_phase3_isolated())
