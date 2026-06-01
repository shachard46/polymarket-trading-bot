"""Live Phase 3 smoke: one DB market → Briefer → A-IQ → Deep Researcher.

Opt-in only (skipped in normal ``pytest`` runs). Use on a host with OpenClaw Gateway,
live briefer/deep_researcher agents, working A-IQ, and a readable polymarket-scraper DB.

Run isolated Phase 3 (no phases 1–2, 4–6):

```bash
export OPENCLAW_PHASE3_LIVE=1
export OPENCLAW_ORCHESTRATOR_MODE=live
export OPENCLAW_VAULT_PATH=/tmp/phase3-live-vault   # optional; inspect artifacts here
export POLYMARKET_DB_PATH=/path/to/polymarket.db    # optional if auto-discovered
# optional: pin one market instead of ingesting the freshest open row
# export OPENCLAW_PHASE3_MARKET_ID=0x...

python tests/orchestrator/test_research_pipeline_integration.py

# or via pytest (same env vars):
pytest tests/orchestrator/test_research_pipeline_integration.py -v -s
```

Agents and ``fetch_research_bundle`` are **not** stubbed. Scraper calls use the real DB.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

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

_BOT_ROOT = Path(__file__).resolve().parents[2]
_DB_NAMES = ("polymarket.db", "polymarket.db.bak.20260322003340")


def _phase3_live_requested() -> bool:
    return os.environ.get(_PHASE3_LIVE_ENV) == "1"


def _market_scraper_roots() -> list[Path]:
    roots: list[Path] = []
    env_root = os.environ.get("MARKET_SCRAPER_ROOT", "").strip()
    if env_root:
        roots.append(Path(env_root).expanduser())
    roots.extend(
        (
            _BOT_ROOT.parent / "market-scarper",
            _BOT_ROOT.parent.parent / "market-scarper",
        )
    )
    seen: set[Path] = set()
    out: list[Path] = []
    for root in roots:
        resolved = root.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if (resolved / "polymarket_tools").is_dir():
            out.append(resolved)
    return out


def _market_scraper_root() -> Path | None:
    roots = _market_scraper_roots()
    return roots[0] if roots else None


def _db_candidates() -> list[Path]:
    explicit = os.environ.get("POLYMARKET_DB_PATH", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return [path] if path.is_file() else []

    candidates: list[Path] = []
    for base in _market_scraper_roots():
        for name in _DB_NAMES:
            path = base / name
            if path.is_file():
                candidates.append(path)
    return candidates


def _poly_scan_on_path() -> bool:
    binary = os.environ.get("POLY_SCAN_BIN", "poly-scan")
    return shutil.which(binary) is not None


def _run_poly_scan_cli(*args: str) -> Any | None:
    binary = os.environ.get("POLY_SCAN_BIN", "poly-scan")
    if not shutil.which(binary):
        return None
    timeout = float(os.environ.get("POLY_SCAN_TIMEOUT_SEC", "60"))
    try:
        completed = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=os.environ.copy(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if completed.returncode != 0:
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None


def _run_poly_scan_via_module(*args: str) -> Any | None:
    root = _market_scraper_root()
    if root is None:
        return None
    cmd = [sys.executable, "-m", "polymarket_tools", *args]
    timeout = float(os.environ.get("POLY_SCAN_TIMEOUT_SEC", "60"))
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=str(root),
            env=os.environ.copy(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if completed.returncode != 0:
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None


def _probe_market_db(db_path: Path) -> bool:
    prev = os.environ.get("POLYMARKET_DB_PATH")
    os.environ["POLYMARKET_DB_PATH"] = str(db_path)
    try:
        if _poly_scan_on_path():
            data = _run_poly_scan_cli("get_open_markets", "--limit", "1")
        else:
            data = _run_poly_scan_via_module("get_open_markets", "--limit", "1")
        return isinstance(data, list) and len(data) > 0
    finally:
        if prev is None:
            os.environ.pop("POLYMARKET_DB_PATH", None)
        else:
            os.environ["POLYMARKET_DB_PATH"] = prev


def _discover_market_db() -> Path | None:
    for path in _db_candidates():
        if _probe_market_db(path):
            return path
    return None


def _wire_scraper_env(monkeypatch: pytest.MonkeyPatch | None = None) -> Path:
    db = _discover_market_db()
    if db is None:
        raise RuntimeError(
            "No readable polymarket DB. Set POLYMARKET_DB_PATH or fix market-scraper checkout."
        )

    def _set(key: str, value: str) -> None:
        if monkeypatch is not None:
            monkeypatch.setenv(key, value)
        else:
            os.environ[key] = value

    _set("POLYMARKET_DB_PATH", str(db))
    _set("OPENCLAW_INGEST_LIMIT", "1")
    if not _poly_scan_on_path():
        if monkeypatch is not None:
            monkeypatch.setattr(scraper, "_run_poly_scan", _run_poly_scan_via_module)
        else:
            scraper._run_poly_scan = _run_poly_scan_via_module  # type: ignore[method-assign]
    return db


def _fetch_one_market_from_db() -> MarketRow:
    _wire_scraper_env()
    rows = scraper.fetch_target_markets()
    if not rows:
        raise RuntimeError("get_open_markets returned no ingestible rows")
    return rows[0]


def _resolve_market_row() -> MarketRow:
    pinned = os.environ.get(_PHASE3_MARKET_ENV, "").strip()
    if pinned:
        _wire_scraper_env()
        row = scraper.fetch_market_row(pinned)
        if row is None:
            raise RuntimeError(f"Could not load market row for {_PHASE3_MARKET_ENV}={pinned!r}")
        return row
    return _fetch_one_market_from_db()


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
        db = _wire_scraper_env()
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
    print(f"db={db}")
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
            f"Set {_PHASE3_LIVE_ENV}=1 for live Phase 3 (real agents + A-IQ). "
            "Or run: python tests/orchestrator/test_research_pipeline_integration.py"
        )

    monkeypatch.setenv(RUNNER_MODE_ENV, RUNNER_MODE_LIVE)
    try:
        require_gateway()
    except Exception as exc:
        pytest.skip(f"OpenClaw gateway not ready: {exc}")

    db = _wire_scraper_env(monkeypatch)
    return db


@pytest.fixture()
def phase3_vault(tmp_path):
    explicit = os.environ.get(VAULT_PATH_ENV, "").strip()
    base = Path(explicit).expanduser().resolve() if explicit else tmp_path
    return _make_vault(base=base)


def test_phase3_live_one_market_from_db(phase3_live_env, phase3_vault):
    """Live Briefer → A-IQ → Deep Researcher on one scraper market (phases 1–2 skipped)."""
    market = _resolve_market_row()
    vault = phase3_vault
    _seed_filter_for_phase3(vault, market)

    hydrated = scraper.fetch_market_row(market.market_id)
    assert hydrated is not None
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
