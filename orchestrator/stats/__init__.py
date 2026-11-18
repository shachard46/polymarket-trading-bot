"""Measurement: calibration, Brier / log-loss, P&L and λ fit over the decision ledger.

Pure functions over ledger rows (dicts); :func:`write_stats` renders ``<vault>/00_System/stats.md``.
Submodules: ``calc`` (maths), ``groups`` (row groupings), ``pnl`` (P&L, tax, offers), ``render`` (stats.md).
"""

from orchestrator.stats.calc import (
    _EPS, _brier_group, _brier_or_none, _num, bootstrap_diff_ci, brier, calibration_table, log_loss,
)
from orchestrator.stats.groups import (
    _COUNTERFACTUAL_STAGES, _COVERAGE_SIGNALS, _GATE_FLAG_REASONS, _INTEG_REASONS, _NON_DECISION,
    _SKIP_STAGES, COVERAGE_STAGE, MIN_FIT_N, OFFER_FINAL_STAGES, SHADOW_STAGE, TRIAGE_SHADOW_STAGE,
    _brier_by_field, _latest_by_market, _latest_scored, brier_by_directive_version,
    brier_by_evidence_source, brier_by_model, brier_by_provider, brier_by_runner_backend, brier_shadow, failure_class_counts,
    false_negative_rate, fit_lambda, gate_flag_stats, independent_sample_stats,
    market_integrity_stats, resolutions, scored_triples, signal_coverage_stats, triage_shadow_stats,
)
from orchestrator.stats.pnl import (
    TAX, _OFFER_GROUPS, _group, _pnl, _pnl_groups, bet_pnl, offers_stats, pnl_summary, realised_pnl,
    resolution_years, tax_by_year,
)
from orchestrator.stats.render import _fmt, _tax_desc, compute_stats, render_stats, write_stats

__all__ = ["brier_by_model", "brier_by_provider", "brier_by_runner_backend", "offers_stats", "bet_pnl", "brier_by_evidence_source", "brier_by_directive_version", "brier_shadow", "triage_shadow_stats", "signal_coverage_stats", "independent_sample_stats", "gate_flag_stats", "market_integrity_stats", "failure_class_counts", "compute_stats", "fit_lambda", "pnl_summary", "realised_pnl", "render_stats", "tax_by_year", "write_stats"]
