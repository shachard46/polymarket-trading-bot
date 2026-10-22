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
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config.trading_constants import MAX_ORDER_USD, PAPER_TRADE_MODE

logger = logging.getLogger(__name__)


class ExecutePolymarketTradeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    market_id: str = Field(min_length=1)
    outcome: Literal["YES", "NO"]
    amount: float = Field(gt=0, le=MAX_ORDER_USD)
    price: float | None = Field(default=None, gt=0, lt=1)  # limit/reference price of the chosen side


class ExecutePolymarketTradeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    transaction_hash: str | None
    paper: bool
    error: str | None


def execute_polymarket_trade(
    market_id: str,
    outcome: str,
    amount: float,
    price: float | None = None,
) -> ExecutePolymarketTradeOutput:
    try:
        args = ExecutePolymarketTradeInput(
            market_id=market_id.strip() if isinstance(market_id, str) else market_id,
            outcome=outcome,
            amount=amount,
            price=price,
        )
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        return ExecutePolymarketTradeOutput(
            success=False,
            transaction_hash=None,
            paper=PAPER_TRADE_MODE,
            error=f"invalid input: {details}",
        )

    market_id, outcome, amount, price = args.market_id, args.outcome, args.amount, args.price
    try:
        if PAPER_TRADE_MODE:
            tx_hash = f"PAPER-{uuid.uuid4().hex[:16].upper()}"
            logger.info(
                "PAPER TRADE: %s on %s for $%.2f @ %s [tx=%s]",
                outcome,
                market_id,
                amount,
                price,
                tx_hash,
            )
            return ExecutePolymarketTradeOutput(
                success=True,
                transaction_hash=tx_hash,
                paper=True,
                error=None,
            )

        return ExecutePolymarketTradeOutput(
            success=False,
            transaction_hash=None,
            paper=PAPER_TRADE_MODE,
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
            paper=PAPER_TRADE_MODE,
            error=str(exc),
        )
