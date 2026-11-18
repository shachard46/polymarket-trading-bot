"""
Configuration constants for the Polymarket Trading Pipeline.
"""
# Allocation & Math Constants (see docs/03_strategy_math.md)
TAX = 0.25                 # Israeli capital-gains rate
# "annual_offset": 25% of NET capital gains per calendar year (losses offset gains within the year);
#                  trade maths is pre-tax. "per_trade": legacy, tax on each winning trade, no offset.
TAX_MODE = "annual_offset"
LAMBDA = 0.3               # shrink of researcher estimate toward market: p_adj = q + LAMBDA*(p - q)
UNCERTAINTY_WIDTH = 0.5    # p_high - p_low at which the shrink is fully removed
MIN_EDGE = 0.04            # minimum (p_adj-based) edge on the chosen side
KELLY_FRAC = 0.25          # fractional Kelly multiplier
F_MAX = 0.10               # max bankroll fraction per trade
LIQ_FRAC = 0.05            # max fraction of market liquidity L a single order may consume
EVENT_CAP = 0.15           # max bankroll fraction exposed to one event
TOTAL_EXPOSURE_CAP = 0.60  # max bankroll fraction exposed overall
MIN_ORDER_USD = 5.0        # orders below this are dropped to 0
MAX_DAYS_TO_RESOLUTION = 60  # gate (applied by caller): skip markets resolving later
MIN_DAYS_TO_RESOLUTION = 7   # gate (applied by caller): skip markets resolving sooner
BANKROLL_USD = 1000.0      # legacy/default bankroll (caller now passes current bankroll)
MAX_ORDER_USD = F_MAX * BANKROLL_USD  # hard per-order ceiling enforced by execute_polymarket_trade

# A-IQ Service (overridable via env vars AIQ_BASE_URL, AIQ_POLL_INTERVAL_SEC, AIQ_TIMEOUT_SEC)
AIQ_BASE_URL = "http://localhost:8000"
AIQ_POLL_INTERVAL_SEC = 2.0
AIQ_TIMEOUT_SEC = 300.0

# Research redesign (Phase 3). The model never sees the market price q; the Hub decides
# whether research diverges from q and whether a result looks like a misread.
RESEARCH_MAX_QUICK_WIDTH = 0.30     # quick range wider than this is "uninformative" -> escalate
RESEARCH_SAMPLES = 3                # independent Deep Researcher samples in the FULL stage
RESEARCH_MAX_SAMPLE_SPREAD = 0.15   # max(p) - min(p) across samples above this -> sample_disagreement
RESEARCH_MAX_Q_GAP = 0.40           # |p_median - q| above this -> probable_misread (paper-trading: wider)
RESEARCH_REFRESH_PRICE_MOVE = 0.08  # q moved this much since research -> re-research
RESEARCH_MIN_TTL_HOURS = 48.0       # refresh TTL = max(48h, days_to_resolution * 24h / 10)
AIQ_CACHE_TTL_SEC = 24 * 3600       # A-IQ query cache lifetime

# Gate manipulation signals (Phase 2, deterministic). Fixed constants, NOT Overseer knobs (not in BOUNDS).
# A missing signal skips its check; it never rejects.
MANIP = {
    "suspicious_move_min_abs_change": 0.15,   # |oneDayPriceChange| at/above this ...
    "suspicious_move_max_vol24_frac": 0.5,    # ... with volume24hr < this * liquidity -> suspicious_move
    "concentrated_top1_share": 0.50,          # top-1 holder share of the top-N total (per side) -> concentrated
    "concentrated_top3_share": 0.75,          # top-3 holder share of the top-N total (per side) -> concentrated
    "wide_spread_max": 0.08,                  # yes_ask - yes_bid above this -> wide_spread
    "holders_limit": 20,                      # data-api /holders top-N
    "holders_timeout_sec": 5.0,
}

# Quick-stage shadow test (Phase 3, measure only): a stable ~10% of quick no_divergence skips run
# the FULL stage anyway; results are ledgered (stage shadow_research) and never traded.
SHADOW_RATE = 0.10
SHADOW_MAX_PER_TICK = 2

# Filter Thresholds
FILTERS = {
    "arbitrage_max_combined_ask": 0.98,
    # Overseer-tunable gate / research knobs (see BOUNDS). Defaults mirror the constants above.
    "gate_min_q": 0.05,                   # gate: q below this is "price_extreme"
    "gate_max_q": 0.95,                   # gate: q above this is "price_extreme"
    "min_days_to_resolution": MIN_DAYS_TO_RESOLUTION,
    "max_days_to_resolution": MAX_DAYS_TO_RESOLUTION,
    "research_max_quick_width": RESEARCH_MAX_QUICK_WIDTH,
    "research_max_sample_spread": RESEARCH_MAX_SAMPLE_SPREAD,
    "research_max_q_gap": RESEARCH_MAX_Q_GAP,
}

# Overseer guard-rails. ONLY these knobs may appear in the directives `filters:` block; sizing
# (LAMBDA, KELLY_FRAC, F_MAX, caps) is deliberately absent and off-limits to the Overseer.
BOUND_MAX_STEP_FRAC = 0.15          # a knob may move at most 15% of its current value per version
BOUNDS = {
    "gate_min_q":                 {"min": 0.03, "max": 0.15, "max_step_frac": BOUND_MAX_STEP_FRAC},
    "gate_max_q":                 {"min": 0.85, "max": 0.97, "max_step_frac": BOUND_MAX_STEP_FRAC},
    "min_days_to_resolution":     {"min": 3,    "max": 21,   "max_step_frac": BOUND_MAX_STEP_FRAC},
    "max_days_to_resolution":     {"min": 30,   "max": 120,  "max_step_frac": BOUND_MAX_STEP_FRAC},
    "research_max_quick_width":   {"min": 0.15, "max": 0.45, "max_step_frac": BOUND_MAX_STEP_FRAC},
    "research_max_sample_spread": {"min": 0.08, "max": 0.25, "max_step_frac": BOUND_MAX_STEP_FRAC},
    "research_max_q_gap":         {"min": 0.20, "max": 0.45, "max_step_frac": BOUND_MAX_STEP_FRAC},
}
MAX_KNOBS_CHANGED_PER_RUN = 2       # more than this in one directives doc -> reject the whole doc
KNOB_COOLDOWN_VERSIONS = 3          # a knob changed at version V may change again at V + 3
AUTO_REVERT_MIN_SCORED = 30         # resolved scored markets a version needs before it is judged
AUTO_REVERT_BRIER_WORSE_BY = 0.02   # current Brier worse than previous by more than this -> revert

# Execution Safety
PAPER_TRADE_MODE = True  # If True, execute_polymarket_trade logs the trade but does NOT hit the live API

# Paper-only sizing overrides: looser while PAPER_TRADE_MODE, to collect outcomes faster for
# fit_lambda; live uses LAMBDA / MIN_EDGE.
PAPER_LAMBDA = 0.5
PAPER_MIN_EDGE = 0.03


def effective_lambda() -> float:
    """PAPER_LAMBDA iff PAPER_TRADE_MODE (read at call time), else LAMBDA."""
    return PAPER_LAMBDA if PAPER_TRADE_MODE else LAMBDA


def effective_min_edge() -> float:
    """PAPER_MIN_EDGE iff PAPER_TRADE_MODE (read at call time), else MIN_EDGE."""
    return PAPER_MIN_EDGE if PAPER_TRADE_MODE else MIN_EDGE

# Orchestrator Scheduling (in seconds)
PIPELINE_INTERVAL_SEC = 5 * 3600       # 5 hours
OVERSEER_INTERVAL_SEC = 24 * 3600      # 24 hours

# Vault directory paths (relative to workspace root)
VAULT_PATHS = {
    "system": "Vault/00_System/",
    "filters": "Vault/01_Filters/",
    "active": "Vault/02_Active_Research/",
    "trades": "Vault/03_Trades/",
    "post_mortem": "Vault/04_Post_Mortems/",
}

# In-place pipeline state keys (filter frontmatter / trade JSON / research frontmatter)
STATUS_KEY = "status"
ERROR_LOG_KEY = "error_log"
STATUS_INACTIVE = "inactive"
BELOW_EDGE_KEY = "below_edge_threshold"
