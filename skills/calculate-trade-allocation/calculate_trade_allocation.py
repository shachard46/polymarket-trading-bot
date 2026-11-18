"""calculate_trade_allocation — skill execution module.

Contract: docs/04_skills_contracts.md §2
Math:     docs/03_strategy_math.md
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from config.trading_constants import (
    EVENT_CAP,
    F_MAX,
    KELLY_FRAC,
    LIQ_FRAC,
    MIN_ORDER_USD,
    TOTAL_EXPOSURE_CAP,
    UNCERTAINTY_WIDTH,
    effective_lambda,
    effective_min_edge,
)


class CalculateTradeAllocationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    p: float = Field(ge=0, le=1)
    p_low: float | None = Field(default=None, ge=0, le=1)
    p_high: float | None = Field(default=None, ge=0, le=1)
    q_yes_ask: float = Field(gt=0, lt=1)
    q_no_ask: float | None = Field(default=None, gt=0, lt=1)
    L: float = Field(gt=0)
    D: float = Field(ge=0)
    bankroll: float = Field(gt=0)
    open_exposure_total: float = Field(default=0.0, ge=0)
    open_exposure_event: float = Field(default=0.0, ge=0)
    lam: float = Field(default_factory=effective_lambda, ge=0, le=1)
    min_edge: float = Field(default_factory=effective_min_edge, ge=0, le=1)

    @model_validator(mode="after")
    def _range(self) -> "CalculateTradeAllocationInput":
        if (self.p_low is None) != (self.p_high is None):
            raise ValueError("p_low and p_high must be given together")
        if self.p_low is not None and self.p_high is not None:
            if self.p_low > self.p_high:
                raise ValueError("p_low must be <= p_high")
            if not (self.p_low <= self.p <= self.p_high):
                raise ValueError("p must lie within [p_low, p_high]")
        return self


class CalculateTradeAllocationOutput(BaseModel):
    """``below_edge_threshold`` is ``True`` iff best edge < ``min_edge``; ``None`` on error."""

    model_config = ConfigDict(extra="forbid")

    allocation_usd: float
    side: str | None
    price: float | None
    shares: float
    p_adj: float
    edge: float
    kelly_fraction: float
    below_edge_threshold: bool | None
    binding_cap: str | None
    edge_per_day: float
    error: str | None
    lam: float | None = None       # shrink actually used
    min_edge: float | None = None  # edge gate actually used


def _error(message: str) -> CalculateTradeAllocationOutput:
    return CalculateTradeAllocationOutput(
        allocation_usd=0.0, side=None, price=None, shares=0.0, p_adj=0.0, edge=0.0,
        kelly_fraction=0.0, below_edge_threshold=None, binding_cap=None,
        edge_per_day=0.0, error=message,
    )


def calculate_trade_allocation(
    p: float,
    q_yes_ask: float | None = None,
    L: float | None = None,
    D: float | None = None,
    bankroll: float | None = None,
    open_exposure_total: float = 0.0,
    open_exposure_event: float = 0.0,
    p_low: float | None = None,
    p_high: float | None = None,
    q_no_ask: float | None = None,
    lam: float | None = None,
    min_edge: float | None = None,
    q: float | None = None,
) -> CalculateTradeAllocationOutput:
    """``q`` is accepted as an alias for ``q_yes_ask``."""
    if q_yes_ask is None:
        q_yes_ask = q
    try:
        args = CalculateTradeAllocationInput(
            p=p, p_low=p_low, p_high=p_high, q_yes_ask=q_yes_ask, q_no_ask=q_no_ask,
            L=L, D=D, bankroll=bankroll,
            open_exposure_total=open_exposure_total,
            open_exposure_event=open_exposure_event,
            **({} if lam is None else {"lam": lam}),
            **({} if min_edge is None else {"min_edge": min_edge}),
        )
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        return _error(f"invalid input: {details}")

    try:
        qy = args.q_yes_ask
        qn = args.q_no_ask if args.q_no_ask is not None else 1.0 - qy
        shrink = args.lam
        if args.p_low is not None and args.p_high is not None:
            w = args.p_high - args.p_low
            shrink *= max(0.0, 1.0 - w / UNCERTAINTY_WIDTH)
        p_adj = qy + shrink * (args.p - qy)

        edge_yes = p_adj - qy
        edge_no = (1.0 - p_adj) - qn
        if edge_yes >= edge_no:
            side, price, p_side, edge = "YES", qy, p_adj, edge_yes
        else:
            side, price, p_side, edge = "NO", qn, 1.0 - p_adj, edge_no
        edge_per_day = edge / max(args.D, 1.0)

        def out(alloc=0.0, kelly=0.0, below=False, cap=None):
            return CalculateTradeAllocationOutput(
                allocation_usd=alloc, side=side, price=price,
                shares=alloc / price if alloc > 0 else 0.0, p_adj=p_adj, edge=edge,
                kelly_fraction=kelly, below_edge_threshold=below, binding_cap=cap,
                edge_per_day=edge_per_day, error=None,
                lam=args.lam, min_edge=args.min_edge,
            )

        if edge < args.min_edge:
            return out(below=True, cap="min_edge")

        # Odds are pre-tax. The annual tax scales wins and losses alike at portfolio level, so it
        # changes neither the sign nor the Kelly fraction; f is deliberately NOT inflated by
        # 1/(1-TAX) (conservative).
        b = (1.0 - price) / price
        f_star = (p_side * b - (1.0 - p_side)) / b
        if f_star <= 0:
            return out(cap="kelly_nonpositive")
        f = KELLY_FRAC * f_star

        B = args.bankroll
        caps = {
            "kelly": f * B,
            "f_max": F_MAX * B,
            "liquidity": LIQ_FRAC * args.L,
            "event": EVENT_CAP * B - args.open_exposure_event,
            "total": TOTAL_EXPOSURE_CAP * B - args.open_exposure_total,
        }
        binding = min(caps, key=caps.get)
        alloc = max(0.0, caps[binding])
        if alloc < MIN_ORDER_USD:
            return out(kelly=f, cap="min_order" if alloc > 0 else binding)
        return out(alloc=alloc, kelly=f, cap=binding)
    except Exception as exc:
        return _error(str(exc))
