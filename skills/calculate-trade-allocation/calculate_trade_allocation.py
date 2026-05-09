"""calculate_trade_allocation — OpenClaw skill execution module.

Contract: docs/04_skills_contracts.md §3
Math:     docs/03_strategy_math.md
"""
from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict

from config.trading_constants import (
    ALPHA,
    BANKROLL_USD,
    BETA,
    C1,
    C2,
    D_MIN,
    EPSILON,
    F_MAX,
    MU_X,
    S_0,
    SIGMA_X,
)


class CalculateTradeAllocationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    p: float
    q: float
    D: int
    L: float
    V: float


class CalculateTradeAllocationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allocation_usd: float
    score: float
    error: str | None


def calculate_trade_allocation(
    p: float,
    q: float,
    D: int,
    L: float,
    V: float,
) -> CalculateTradeAllocationOutput:
    try:
        # Step 1: expected return after 25% tax on winnings
        e_tax = (0.75 * p - q + 0.25 * p * q) / q

        # Step 2: time adjustment — penalise long capital lock-up
        time_adj = e_tax / max(D, D_MIN)

        # Step 3: rarity bonus — rewards statistical outliers in p vs q
        z = (abs(p - q) - MU_X) / (SIGMA_X + EPSILON)
        B_rarity = 1.0 + BETA * math.tanh(max(0.0, z))

        # Step 4: execution penalty for thin markets
        P_exec = C1 / math.log1p(L) + C2 / math.log1p(V)

        # Step 5: final score
        S = time_adj * B_rarity - P_exec

        # Step 6: bankroll fraction → USD allocation
        f = 0.0 if S <= S_0 else min(F_MAX, ALPHA * (S - S_0))
        allocation_usd = f * BANKROLL_USD

        return CalculateTradeAllocationOutput(
            allocation_usd=allocation_usd,
            score=S,
            error=None,
        )
    except Exception as exc:
        return CalculateTradeAllocationOutput(
            allocation_usd=0.0,
            score=0.0,
            error=str(exc),
        )
