"""execute_polymarket_trade — OpenClaw skill execution module.

Contract: docs/04_skills_contracts.md §4

When PAPER_TRADE_MODE is True (the default), the skill logs the intent and returns
a mock success response without touching any live endpoint.
Live trading requires Polymarket CLOB API private-key signing and is intentionally
gated behind PAPER_TRADE_MODE=False — see config/trading_constants.py.
"""
from __future__ import annotations

import logging
import uuid

from pydantic import BaseModel, ConfigDict

from config.trading_constants import PAPER_TRADE_MODE

logger = logging.getLogger(__name__)


class ExecutePolymarketTradeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market_id: str
    outcome: str
    amount: float


class ExecutePolymarketTradeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    transaction_hash: str | None
    error: str | None


def execute_polymarket_trade(
    market_id: str,
    outcome: str,
    amount: float,
) -> ExecutePolymarketTradeOutput:
    try:
        if PAPER_TRADE_MODE:
            tx_hash = f"PAPER-{uuid.uuid4().hex[:16].upper()}"
            logger.info(
                "PAPER TRADE: %s on %s for $%.2f [tx=%s]",
                outcome,
                market_id,
                amount,
                tx_hash,
            )
            return ExecutePolymarketTradeOutput(
                success=True,
                transaction_hash=tx_hash,
                error=None,
            )

        return ExecutePolymarketTradeOutput(
            success=False,
            transaction_hash=None,
            error=(
                "Live trading not implemented: requires Polymarket CLOB API "
                "private-key signing (EIP-712). Set PAPER_TRADE_MODE=True in "
                "config/trading_constants.py to run in simulation."
            ),
        )
    except Exception as exc:
        return ExecutePolymarketTradeOutput(
            success=False,
            transaction_hash=None,
            error=str(exc),
        )
