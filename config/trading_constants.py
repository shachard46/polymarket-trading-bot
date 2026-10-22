"""
Configuration constants for the Polymarket Trading Pipeline.
"""
# Allocation & Math Constants (see docs/03_strategy_math.md)
TAX = 0.25                 # tax on WINNINGS (profit) only; losses do not offset
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

# Filter Thresholds
FILTERS = {
    "volume_shock_ma_multiplier": 2.5,   # was 2.0 — require a stronger volume spike
    "breakout_pct_shift": 0.08,           # was 0.06 — cut routine 4h drift
    "breakout_time_window_hrs": 4,
    "spread_anomaly_multiplier": 2.0,    # was 1.5 — proper anomaly, not just wider
    "info_drift_sequential_trades": 9,   # was 7 — tighter consecutive-move streak
    "low_liquidity_breakout_max_liq": 2000,
    "low_liquidity_breakout_pct": 0.05,  # was 0.03 — raise thin-market bar
    "low_liquidity_dead_window_hrs": 48,
    "arbitrage_max_combined_ask": 0.98
}

# Execution Safety
PAPER_TRADE_MODE = True  # If True, executioner logs trade but does NOT hit live API

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
