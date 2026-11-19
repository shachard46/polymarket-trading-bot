#!/usr/bin/env python3
"""Backtest the FULL research stage on recently resolved Polymarket markets.

For each resolved binary market: pick a cutoff date (end_date - K days), run research as if today were the cutoff
(Brave freshness limited to ``YYYY-MM-DDtoYYYY-MM-DD`` ending at the cutoff), record p vs the real outcome, and report
Brier and log loss per pipeline tag. The research function is injectable: research(row, today, search_freshness) -> {p, ...}.

LEAKAGE CAVEATS (also written to the report):
  * Brave freshness filters by page age, which can be wrong or updated after publication; a page edited after the
    cutoff may still carry an older date.
  * The research LLM's training data may already contain the outcome (model cutoff). Nothing here can remove that.
  * Gemini grounding cannot be date-limited, so without BRAVE_API_KEY the run refuses unless --allow-leaky.
  * Markets are selected by having resolved, so selection is not random with respect to the outcome.
Treat results as a noisy upper bound on skill, and compare pipelines on the same markets only.

Usage:
  python3 scripts/backtest.py --dry-run
  BRAVE_API_KEY=... python3 scripts/backtest.py --max-markets 10 --days-before 7
  python3 scripts/backtest.py --research-fn old=mypkg.mod:fn --research-fn crux_v1=mypkg.mod:fn2
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

log = logging.getLogger("backtest")

DEFAULT_GAMMA = "https://gamma-api.polymarket.com"
DEFAULT_TAG = "crux_v1"
DEFAULT_ENTRY = "orchestrator.phases.p3_research:backtest_research"
LOG_EPS = 1e-6
LEAKAGE_CAVEATS = [
    "Brave freshness filters by page age; pages edited after the cutoff can carry an older date.",
    "The research model's training data may contain the outcome; this cannot be removed.",
    "Gemini grounding cannot be date-limited (only Brave freshness is used for the cutoff).",
    "Markets are chosen because they resolved: selection is not random w.r.t. the outcome.",
]

ResearchFn = Callable[[dict, str, str], dict]


def _json_list(v: Any) -> list:
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v) if isinstance(v, str) else []
    except ValueError:
        return []
    return out if isinstance(out, list) else []


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def outcome_yes(raw: dict) -> int | None:
    """1/0 when a binary Yes/No market settled cleanly, else None (non-binary, void, unsettled)."""
    labels = [str(x).strip().lower() for x in _json_list(raw.get("outcomes"))]
    if sorted(labels) != ["no", "yes"]:
        return None
    try:
        prices = [float(x) for x in _json_list(raw.get("outcomePrices"))]
    except (TypeError, ValueError):
        return None
    if len(prices) != 2:
        return None
    py, pn = prices[labels.index("yes")], prices[labels.index("no")]
    if py >= 0.99 and pn <= 0.01:
        return 1
    if py <= 0.01 and pn >= 0.99:
        return 0
    return None


def fetch_resolved_markets(
    *, days_back: int, days_before: int, limit: int, now: datetime | None = None, base: str | None = None
) -> list[dict]:
    """Recently resolved binary markets from Gamma (closed=true, end date inside the last ``days_back`` days)."""
    now = now or datetime.now(timezone.utc)
    base = (base or os.environ.get("GAMMA_BASE_URL") or DEFAULT_GAMMA).rstrip("/")
    params = {
        "closed": "true",
        "end_date_min": (now - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_date_max": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "order": "volume",
        "ascending": "false",
        "limit": 100,
    }
    out: list[dict] = []
    offset = 0
    for _ in range(10):
        resp = requests.get(f"{base}/markets", params={**params, "offset": offset}, timeout=20)
        if resp.status_code != 200:
            raise RuntimeError(f"Gamma HTTP {resp.status_code}")
        page = resp.json()
        if not isinstance(page, list) or not page:
            break
        for raw in page:
            m = market_from_gamma(raw, days_before)
            if m:
                out.append(m)
                if len(out) >= limit:
                    return out
        offset += len(page)
        if len(page) < params["limit"]:
            break
    return out


def market_from_gamma(raw: dict, days_before: int) -> dict | None:
    """Backtest market dict from a Gamma row, or None when unusable (non-binary, unclear outcome, cutoff before the market existed)."""
    y = outcome_yes(raw)
    end = _parse_dt(raw.get("endDate"))
    mid = raw.get("conditionId") or raw.get("id")
    if y is None or end is None or not mid:
        return None
    cutoff = (end - timedelta(days=days_before)).date()
    start = _parse_dt(raw.get("startDate") or raw.get("createdAt"))
    if start is not None and cutoff <= start.date():
        return None
    return {
        "market_id": str(mid),
        "question": raw.get("question") or "",
        "rules": raw.get("description") or "",
        "end_date": end.isoformat(),
        "cutoff": cutoff.isoformat(),
        "outcome_yes": y,
    }


def freshness_range(cutoff: str, lookback_days: int) -> str:
    c = date.fromisoformat(cutoff)
    return f"{(c - timedelta(days=lookback_days)).isoformat()}to{c.isoformat()}"


def load_research_fn(spec: str) -> ResearchFn:
    mod, _, attr = spec.partition(":")
    try:
        fn = getattr(importlib.import_module(mod), attr)
    except (ImportError, AttributeError) as exc:
        raise SystemExit(f"cannot load research function {spec!r}: {exc}")
    return fn


def default_research(row: dict, today: str, search_freshness: str) -> dict:
    """Default FULL-stage entry, imported lazily; must expose backtest_research(row, today, search_freshness)."""
    fn = load_research_fn(DEFAULT_ENTRY)
    from orchestrator import brave_search

    with brave_search.freshness_override(search_freshness):
        return fn(row, today, search_freshness)


def brier(pairs: list[tuple[float, int]]) -> float | None:
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else None


def log_loss(pairs: list[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    tot = 0.0
    for p, y in pairs:
        p = min(max(p, LOG_EPS), 1 - LOG_EPS)
        tot -= math.log(p if y else 1 - p)
    return tot / len(pairs)


def run_backtest(
    markets: list[dict],
    fns: dict[str, ResearchFn],
    *,
    lookback_days: int,
) -> list[dict]:
    """One result per (market, pipeline tag). A research failure is recorded as an error row, never raised."""
    results: list[dict] = []
    for m in markets:
        fresh = freshness_range(m["cutoff"], lookback_days)
        row = {k: m[k] for k in ("market_id", "question", "rules", "end_date")}
        row["title"] = m["question"]
        for tag, fn in fns.items():
            rec = {"market_id": m["market_id"], "question": m["question"], "cutoff": m["cutoff"],
                   "outcome_yes": m["outcome_yes"], "freshness": fresh, "pipeline": tag, "p": None, "error": None}
            try:
                out = fn(dict(row), m["cutoff"], fresh) or {}
                p = out.get("p")
                if p is None:
                    rec["error"] = str(out.get("error") or out.get("skip") or "no p returned")
                else:
                    rec["p"] = float(p)
                    rec["detail"] = {k: v for k, v in out.items() if k != "p"}
                    rec["pipeline"] = str(out.get("pipeline") or tag)
            except Exception as exc:  # noqa: BLE001 - one market must not stop the run
                rec["error"] = f"{type(exc).__name__}: {exc}"
            log.info("backtest %s [%s] p=%s outcome=%s err=%s", m["market_id"], rec["pipeline"], rec["p"], m["outcome_yes"], rec["error"])
            results.append(rec)
    return results


def summarize(results: list[dict]) -> dict[str, dict]:
    by: dict[str, list[dict]] = {}
    for r in results:
        by.setdefault(r["pipeline"], []).append(r)
    out = {}
    for tag, rows in sorted(by.items()):
        pairs = [(r["p"], r["outcome_yes"]) for r in rows if r["p"] is not None]
        out[tag] = {"n": len(pairs), "errors": len(rows) - len(pairs), "brier": brier(pairs), "log_loss": log_loss(pairs)}
    return out


def render_report(summary: dict, results: list[dict], meta: dict) -> str:
    f = lambda v: "n/a" if v is None else f"{v:.4f}"
    lines = ["# Backtest report", "", f"Run: {meta['run_at']}  |  markets: {meta['markets']}  |  days before end: {meta['days_before']}  |  leaky: {meta['leaky']}", "",
             "| pipeline | n | errors | Brier | log loss |", "|---|---|---|---|---|"]
    for tag, s in summary.items():
        lines.append(f"| {tag} | {s['n']} | {s['errors']} | {f(s['brier'])} | {f(s['log_loss'])} |")
    lines += ["", "Reference: always-0.5 Brier is 0.2500, log loss 0.6931.", "", "## Per market", "",
              "| market | pipeline | cutoff | p | outcome | error |", "|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['question'][:70].replace('|', '/')} | {r['pipeline']} | {r['cutoff']} | {f(r['p'])} | {r['outcome_yes']} | {r['error'] or ''} |")
    lines += ["", "## Leakage caveats", ""] + [f"- {c}" for c in LEAKAGE_CAVEATS]
    if meta["leaky"]:
        lines.append("- LEAKY RUN: no BRAVE_API_KEY, search is NOT date-limited; results are not a valid backtest.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, *, research_fn: ResearchFn | None = None, markets: list[dict] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--max-markets", type=int, default=10, help="cost guard (default 10)")
    ap.add_argument("--days-before", type=int, default=7, help="cutoff = end_date - K days (default 7)")
    ap.add_argument("--days-back", type=int, default=30, help="markets that ended within the last N days (default 30)")
    ap.add_argument("--lookback-days", type=int, default=180, help="Brave freshness window length before the cutoff")
    ap.add_argument("--research-fn", action="append", default=[], metavar="[TAG=]module:attr", help="repeatable; default FULL stage")
    ap.add_argument("--results-dir", default=str(ROOT / "logs" / "backtest"))
    ap.add_argument("--dry-run", action="store_true", help="list markets and cutoffs; no research, no cost")
    ap.add_argument("--allow-leaky", action="store_true", help="run without BRAVE_API_KEY (search not date-limited)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.max_markets < 1 or args.days_before < 1:
        ap.error("--max-markets and --days-before must be >= 1")

    leaky = not os.environ.get("BRAVE_API_KEY", "").strip()
    if leaky and not args.dry_run and not args.allow_leaky:
        print("refusing: BRAVE_API_KEY not set and Gemini cannot date-limit search (use --allow-leaky to override)", file=sys.stderr)
        return 2
    for c in LEAKAGE_CAVEATS:
        log.warning("[LEAKAGE] %s", c)
    if leaky:
        log.warning("[LEAKAGE] no BRAVE_API_KEY: search is not date-limited")

    if markets is None:
        markets = fetch_resolved_markets(days_back=args.days_back, days_before=args.days_before, limit=args.max_markets)
    markets = markets[: args.max_markets]
    if args.dry_run:
        for m in markets:
            print(f"{m['market_id']} cutoff={m['cutoff']} end={m['end_date'][:10]} outcome={m['outcome_yes']} freshness={freshness_range(m['cutoff'], args.lookback_days)} | {m['question']}")
        print(f"dry-run: {len(markets)} markets, no research run")
        return 0

    fns: dict[str, ResearchFn] = {}
    if research_fn is not None:
        fns[DEFAULT_TAG] = research_fn
    for spec in args.research_fn:
        tag, sep, target = spec.partition("=")
        if not sep:
            tag, target = DEFAULT_TAG, spec
        fns[tag] = load_research_fn(target)
    if not fns:
        fns[DEFAULT_TAG] = default_research

    results = run_backtest(markets, fns, lookback_days=args.lookback_days)
    summary = summarize(results)
    run_at = datetime.now(timezone.utc)
    meta = {"run_at": run_at.isoformat(timespec="seconds"), "markets": len(markets), "days_before": args.days_before, "leaky": leaky}
    out = Path(args.results_dir) / run_at.strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps({"meta": meta, "summary": summary, "results": results, "caveats": LEAKAGE_CAVEATS}, indent=2, default=str))
    (out / "report.md").write_text(render_report(summary, results, meta))
    print(f"wrote {out}")
    for tag, s in summary.items():
        print(f"{tag}: n={s['n']} errors={s['errors']} brier={s['brier']} log_loss={s['log_loss']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
