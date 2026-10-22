"""calculate_trade_allocation — OpenClaw skill execution module.

Contract: docs/04_skills_contracts.md §3
Math:     docs/03_strategy_math.md
"""
from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config.trading_constants import (
    ALPHA,
    BANKROLL_USD,
    BETA,
    C1,
    C2,
    D_MIN,
    EPSILON,
    F_MAX,
    LIQ_FRAC,
    MU_X,
    S_0,
    SIGMA_X,
)


class CalculateTradeAllocationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    p: float = Field(gt=0, le=1)
    q: float = Field(gt=0, lt=1)
    D: int = Field(ge=0)
    L: float = Field(gt=0)
    V: float = Field(gt=0)
    remaining_bankroll: float | None = Field(default=None, ge=0)


class CalculateTradeAllocationOutput(BaseModel):
    """``below_edge_threshold`` is ``True`` iff ``S <= S_0`` (edge gate); ``None`` on tool error."""

    model_config = ConfigDict(extra="forbid")

    allocation_usd: float
    score: float
    below_edge_threshold: bool | None
    error: str | None


def _error(message: str) -> CalculateTradeAllocationOutput:
    return CalculateTradeAllocationOutput(
        allocation_usd=0.0,
        score=0.0,
        below_edge_threshold=None,
        error=message,
    )


def calculate_trade_allocation(
    p: float,
    q: float,
    D: int,
    L: float,
    V: float,
    remaining_bankroll: float | None = None,
) -> CalculateTradeAllocationOutput:
    try:
        args = CalculateTradeAllocationInput(
            p=p, q=q, D=D, L=L, V=V, remaining_bankroll=remaining_bankroll
        )
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        return _error(f"invalid input: {details}")

    try:
        p, q, D, L, V = args.p, args.q, args.D, args.L, args.V
        remaining = (
            BANKROLL_USD if args.remaining_bankroll is None else args.remaining_bankroll
        )

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

        if not math.isfinite(S):
            return _error(f"non-finite score: {S}")

        # Step 6: bankroll fraction → USD allocation, capped by per-trade
        # bankroll fraction, market liquidity share, and remaining bankroll.
        below_edge = not (S > S_0)
        if below_edge:
            allocation_usd = 0.0
        else:
            f = min(F_MAX, ALPHA * (S - S_0))
            allocation_usd = min(
                f * BANKROLL_USD,
                F_MAX * BANKROLL_USD,
                LIQ_FRAC * L,
                remaining,
            )

        return CalculateTradeAllocationOutput(
            allocation_usd=allocation_usd,
            score=S,
            below_edge_threshold=below_edge,
            error=None,
        )
    except Exception as exc:
        return _error(str(exc))
