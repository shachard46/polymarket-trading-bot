"""compute_stats, stats.md rendering (one renderer per section) and write_stats."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from config import trading_constants
from obsidian_utils import ObsidianManager
from orchestrator import ledger
from orchestrator.stats.calc import bootstrap_diff_ci, brier, calibration_table, log_loss
from orchestrator.stats.groups import (
    _COUNTERFACTUAL_STAGES,
    brier_by_evidence_source,
    brier_by_model,
    brier_by_provider,
    brier_by_runner_backend,
    brier_shadow,
    false_negative_rate,
    fit_lambda,
    gate_flag_stats,
    market_integrity_stats,
    resolutions,
    scored_triples,
    signal_coverage_stats,
    triage_shadow_stats,
)
from orchestrator.stats.cost import cost_stats, web_filter_stats
from orchestrator.stats.pnl import TAX, _group, _pnl_groups, offers_stats, pnl_summary


def compute_stats(
    rows: list[dict[str, Any]], usage: list[dict[str, Any]] | None = None,
    web_filter: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Every statistic over ``rows`` (ledger rows, scanned once by the caller) and LLM ``usage`` rows."""
    res = resolutions(rows)
    triples = scored_triples(rows)
    out: dict[str, Any] = {"n_resolved": len(res), "n_scored": len(triples)}
    if triples:
        ps = [t[0] for t in triples]
        qs = [t[1] for t in triples]
        ys = [t[2] for t in triples]
        out.update(
            brier_p=brier(ps, ys),
            brier_q=brier(qs, ys),
            logloss_p=log_loss(ps, ys),
            logloss_q=log_loss(qs, ys),
            brier_diff=brier(ps, ys) - brier(qs, ys),
            brier_diff_ci=bootstrap_diff_ci(triples),
        )
    out["brier_shadow"] = brier_shadow(rows)
    out["triage_shadow"] = triage_shadow_stats(rows)
    out["signal_coverage"] = signal_coverage_stats(rows)
    out["gate_flags"] = gate_flag_stats(rows)
    out["market_integrity"] = market_integrity_stats(rows)
    out["calibration"] = calibration_table(triples)
    out["brier_by_evidence_source"] = brier_by_evidence_source(rows)
    out["brier_by_runner_backend"] = brier_by_runner_backend(rows)
    out["brier_by_model"] = brier_by_model(rows)
    out["brier_by_provider"] = brier_by_provider(rows)
    out["offers"] = offers_stats(rows)
    traded = _pnl_groups(rows, res, ("traded",))
    summ = pnl_summary(rows, ("traded",))
    out["pnl_total"] = summ["pre_tax"]
    out["pnl_tax"] = summ["tax"]
    out["pnl_after_tax"] = summ["after_tax"]
    out["pnl_tax_by_year"] = summ["tax_by_year"]
    out["pnl_n"] = len(traded)
    out["pnl_by_category"] = _group(traded, "category")
    out["pnl_by_reason"] = _group(traded, "reason")
    cf = _pnl_groups(rows, res, _COUNTERFACTUAL_STAGES, default_size=1.0)
    out["counterfactual_by_stage"] = _group(cf, "stage")
    out["counterfactual_after_tax"] = pnl_summary(rows, _COUNTERFACTUAL_STAGES, default_size=1.0)
    out["false_negative"] = false_negative_rate(rows)
    out["lambda"] = fit_lambda(rows)
    out["cost"] = cost_stats(rows, usage or [])
    out["web_filter"] = web_filter_stats(web_filter or [])
    return out


def _fmt(x: Any, nd: int = 4) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def _tax_desc() -> str:
    return f"annual_offset: {int(TAX * 100)}% of net gains per calendar year, losses offset within the year"


def _render_model_vs_market(s: dict[str, Any]) -> list[str]:
    L = [
        "# Stats",
        "",
        f"_Generated {datetime.now(tz=timezone.utc).isoformat()}_",
        "",
        f"Resolved markets: **{s['n_resolved']}** (scored with p and q: {s['n_scored']})",
        "",
        "## Model vs market",
        "",
    ]
    if "brier_p" in s:
        lo, hi = s["brier_diff_ci"]
        L += [
            "| metric | model (p) | market (q) |",
            "|---|---|---|",
            f"| Brier | {_fmt(s['brier_p'])} | {_fmt(s['brier_q'])} |",
            f"| Log-loss | {_fmt(s['logloss_p'])} | {_fmt(s['logloss_q'])} |",
            "",
            f"Brier(p) - Brier(q) = {_fmt(s['brier_diff'])}, bootstrap 90% CI [{_fmt(lo)}, {_fmt(hi)}] "
            "(negative = model better).",
        ]
    else:
        L.append("No resolved markets with both p and q yet.")
    return L


def _render_brier_by(s: dict[str, Any], title: str, field: str, key: str) -> list[str]:
    L = ["", f"### Brier by {title}", "", f"| {field} | n | Brier(p) | Brier(p) - Brier(q) |", "|---|---|---|---|"]
    L += [f"| {k} | {n} | {_fmt(bp)} | {_fmt(d)} |" for k, (n, bp, d) in s.get(key, {}).items()]
    return L


def _render_shadow(s: dict[str, Any]) -> list[str]:
    n_sh, b_quick, b_full, b_mkt = s.get("brier_shadow", (0, None, None, None))
    return [
        "", "### Shadow: quick vs full vs market", "",
        f"Resolved shadow markets (quick no_divergence skips run through FULL anyway): {n_sh}.",
        "", "| quick p | full p | market q |", "|---|---|---|",
        f"| {_fmt(b_quick)} | {_fmt(b_full)} | {_fmt(b_mkt)} |",
    ]


def _render_signal_coverage(s: dict[str, Any]) -> list[str]:
    L = ["", "### Signal coverage (missing / total of gated markets)", "",
         "| signal | latest tick | % | last 7 days | % |", "|---|---|---|---|---|"]
    alert = trading_constants.SIGNAL_COVERAGE_ALERT_FRAC
    for sig, d in s.get("signal_coverage", {}).get("signals", {}).items():
        cells = []
        for m, t in (d["latest"], d["week"]):
            frac = m / t if t else None
            flag = " ⚠" if frac is not None and frac > alert else ""
            cells += [f"{m}/{t}", f"{_fmt(None if frac is None else 100 * frac, 1)}%{flag}"]
        L.append(f"| {sig} | " + " | ".join(cells) + " |")
    return L


def _render_triage_shadow(s: dict[str, Any]) -> list[str]:
    L = ["", "### Triage shadow: quick p vs market (resolved triage rejects)", "",
         "| reason | n | Brier(quick p) | Brier(q) |", "|---|---|---|---|"]
    for kind, (n, bp, bq) in s.get("triage_shadow", {}).items():
        L.append(f"| {kind} | {n} | {_fmt(bp)} | {_fmt(bq)} |")
    return L


def _render_gate_flags(s: dict[str, Any]) -> list[str]:
    L = ["", "### Gate flags vs market calibration", "",
         "Brier(q) on resolved markets, by gate flag vs markets that passed the gate.", "",
         "| group | n | Brier(q) |", "|---|---|---|"]
    for key, (n, bq) in s.get("gate_flags", {}).items():
        L.append(f"| {key} | {n} | {_fmt(bq)} |")
    return L


def _render_integrity(s: dict[str, Any]) -> list[str]:
    mi = s.get("market_integrity") or {"bands": {}, "rejects": {}}
    L = ["", "### Market integrity", "",
         "Brier(q) on resolved markets by integrity score band, and by integrity gate-reject reason.", "",
         "| group | n | Brier(q) |", "|---|---|---|"]
    for key, (n, bq) in mi["bands"].items():
        L.append(f"| score {key} | {n} | {_fmt(bq)} |")
    for key, (n, bq) in mi["rejects"].items():
        L.append(f"| gate_reject {key} | {n} | {_fmt(bq)} |")
    return L


def _render_offers(s: dict[str, Any]) -> list[str]:
    off = s.get("offers") or {"pending": 0, "counts": {}, "groups": {}}
    L = ["", "## Offers & votes", "", f"Pending: {off['pending']}. Approved {off['counts'].get('approved', 0)}, "
         f"declined {off['counts'].get('declined', 0)}, expired {off['counts'].get('expired', 0)}, "
         f"stale {off['counts'].get('stale', 0)}.", "",
         "| group | n resolved | Brier(p) | Brier(q) | hypothetical P&L USD |", "|---|---|---|---|---|"]
    for g, v in off["groups"].items():
        L.append(f"| {g} | {v['n_resolved']} | {_fmt(v['brier_p'])} | {_fmt(v['brier_q'])} | {v['pnl']:.2f} |")
    return L


def _render_calibration(s: dict[str, Any]) -> list[str]:
    L = ["", "## Calibration (p in 10% buckets)", "", "| bucket | n | mean p | hit rate |", "|---|---|---|---|"]
    L += [f"| {c['bucket']} | {c['n']} | {_fmt(c['mean_p'], 3)} | {_fmt(c['hit_rate'], 3)} |" for c in s["calibration"]]
    return L


def _render_pnl(s: dict[str, Any]) -> list[str]:
    L = [
        "",
        f"## Realised P&L (traded; {_tax_desc()})",
        "",
        f"Pre-tax: **{s['pnl_total']:.2f} USD** over {s['pnl_n']} resolved trades; "
        f"tax: {s['pnl_tax']:.2f} USD; after-tax: **{s['pnl_after_tax']:.2f} USD**.",
    ]
    L += [f"- {y}: tax {t:.2f} USD" for y, t in s["pnl_tax_by_year"].items()]
    for title, key in (("Per category", "pnl_by_category"), ("Per stage reason", "pnl_by_reason")):
        L += ["", f"### {title}", "", "| key | n | P&L USD |", "|---|---|---|"]
        L += [f"| {k} | {n} | {v:.2f} |" for k, (n, v) in s[key].items()]
    return L


def _render_counterfactual(s: dict[str, Any]) -> list[str]:
    L = [
        "",
        "## Counterfactual P&L (hypothetical, at stated side/price; 1 USD stake if size missing)",
        "",
        "| stage | n | P&L USD |",
        "|---|---|---|",
    ]
    L += [f"| {k} | {n} | {v:.2f} |" for k, (n, v) in s["counterfactual_by_stage"].items()]
    cfs = s["counterfactual_after_tax"]
    L += ["", f"Counterfactual total: pre-tax {cfs['pre_tax']:.2f}, tax {cfs['tax']:.2f}, after-tax {cfs['after_tax']:.2f} USD."]
    return L


def _render_false_negative_and_lambda(s: dict[str, Any]) -> list[str]:
    n_fn, rate = s["false_negative"]
    return [
        "",
        "## False-negative rate (PROXY)",
        "",
        f"Among {n_fn} resolved gate/triage/research rejects with q, share where |outcome - q| > 0.5: "
        f"{_fmt(rate, 3)}. This is a cheap proxy, not a true missed-edge rate.",
        "",
        "## Fitted lambda",
        "",
        f"argmin Brier of q + lambda(p - q): {_fmt(s['lambda'], 2)}"
        + (" (needs n >= 50)" if s["lambda"] is None else ""),
        "",
    ]


def _render_cost(s: dict[str, Any]) -> list[str]:
    c = s.get("cost")
    L = ["", "## LLM cost", ""]
    if not c or not c["n_calls"]:
        return L + ["No LLM usage rows yet.", ""]
    L += [f"Today: **{c['today']:.4f} USD**; all time: {c['total']:.4f} USD over {c['n_calls']} calls.", "",
          "| day | cost USD |", "|---|---|"]
    L += [f"| {d} | {v:.4f} |" for d, v in c["per_day"].items()]
    think = c.get("thinking_by_role") or {}
    L += ["", "### Cost by role", "", "| role | cost USD | thinking tokens |", "|---|---|---|"]
    L += [f"| {k} | {v:.4f} | {think.get(k, 0)} |" for k, v in c["by_role"].items()]
    L += ["", "### Cost by model", "", "| model | cost USD |", "|---|---|"]
    L += [f"| {k} | {v:.4f} |" for k, v in c["by_model"].items()]
    L += ["", "### Cost per researched market", "", "| market | cost USD |", "|---|---|"]
    L += [f"| {k} | {v:.4f} |" for k, v in sorted(c["by_market"].items(), key=lambda kv: -kv[1])]
    L += ["", "### Brier per dollar (by model)", "",
          "| model | n scored | Brier(p) - Brier(q) | cost per scored market USD |", "|---|---|---|---|"]
    for m, (n, diff, cpm) in c["brier_per_dollar"].items():
        L.append(f"| {m} | {n} | {_fmt(diff)} | {_fmt(cpm)} |")
    L += ["", f"Rows with unknown price: {c['n_unknown']}."]
    L += [f"- price missing for {m}" for m in c["missing_models"]]
    return L + [""]


def _render_web_filter(s: dict[str, Any]) -> list[str]:
    w = s.get("web_filter")
    L = ["", "## Web evidence filter", ""]
    if not w or not w["n_calls"]:
        return L + ["No web evidence filter rows yet.", ""]
    t = w["total"]
    L.append(
        f"Total: kept {t['kept']}, dropped (no url) {t['dropped_no_url']}, "
        f"dropped (unretrieved url) {t['dropped_unretrieved']}, dropped (no citations) {t['dropped_unverifiable']}; drop rate {_fmt(t['drop_rate'])}."
    )
    L += ["", "| model | kept | dropped no url | dropped unretrieved | dropped no citations | drop rate |", "|---|---|---|---|---|---|"]
    for m, v in w["by_model"].items():
        L.append(f"| {m} | {v['kept']} | {v['dropped_no_url']} | {v['dropped_unretrieved']} | {v['dropped_unverifiable']} | {_fmt(v['drop_rate'])} |")
    return L + [""]


def render_stats(s: dict[str, Any]) -> str:
    L = _render_model_vs_market(s)
    L += _render_brier_by(s, "evidence source", "evidence_source", "brier_by_evidence_source")
    L += _render_brier_by(s, "runner backend", "runner_backend", "brier_by_runner_backend")
    L += _render_brier_by(s, "model", "model", "brier_by_model")
    L += _render_brier_by(s, "provider", "provider", "brier_by_provider")
    for section in (
        _render_shadow, _render_signal_coverage, _render_triage_shadow,
        _render_gate_flags, _render_integrity, _render_offers, _render_calibration,
        _render_pnl, _render_counterfactual, _render_false_negative_and_lambda, _render_cost,
        _render_web_filter,
    ):
        L += section(s)
    return "\n".join(L)


def write_stats(vault: ObsidianManager, rows: list[dict[str, Any]] | None = None) -> None:
    """Render ``00_System/stats.md``; pass ``rows`` to reuse an already-scanned ledger."""
    if rows is None:
        rows = list(ledger.iter_rows(vault))
    from orchestrator.llm.usage import iter_usage, iter_web_filter

    path = vault._dirs["system"] / "stats.md"
    usage = iter_usage(vault._base)
    path.write_text(render_stats(compute_stats(rows, usage, iter_web_filter(vault._base))), encoding="utf-8")
