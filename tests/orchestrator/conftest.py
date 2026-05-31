"""Shared orchestrator test fixtures."""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.evaluator_output import set_fetch_signal_bundle_impl


@pytest.fixture(autouse=True)
def _stub_evaluator_signal_bundle_fetch():
    """Avoid live DB access when phase tests merge quantitative ``signal_bundle``."""

    def fake_fetch(
        market_id: str,
        filter_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "market_id": market_id,
            "stub": True,
            "signals": {},
            "error": None,
        }

    set_fetch_signal_bundle_impl(fake_fetch)
    yield
    set_fetch_signal_bundle_impl(None)
