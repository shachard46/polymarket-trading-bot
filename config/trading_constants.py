"""
Configuration constants for the Polymarket Trading Pipeline.
"""
# Allocation & Math Constants
BETA = 0.5
C1 = 0.02
C2 = 0.01
D_MIN = 7
S_0 = 0.002
ALPHA = 25
F_MAX = 0.95
EPSILON = 1e-8
MU_X = 0.05            # Historical mean of |p - q| for rarity bonus
SIGMA_X = 0.10         # Historical std-dev of |p - q| for rarity bonus
BANKROLL_USD = 1000.0  # allocation_usd = f * BANKROLL_USD

# A-IQ Service (overridable via env vars AIQ_BASE_URL, AIQ_POLL_INTERVAL_SEC, AIQ_TIMEOUT_SEC)
AIQ_BASE_URL = "http://localhost:8000"
AIQ_POLL_INTERVAL_SEC = 2.0
AIQ_TIMEOUT_SEC = 120.0

# Filter Thresholds
FILTERS = {
    "volume_shock_ma_multiplier": 3.0,
    "breakout_pct_shift": 0.10,
    "breakout_time_window_hrs": 4,
    "spread_anomaly_multiplier": 2.0,
    "info_drift_sequential_trades": 10,
    "low_liquidity_breakout_max_liq": 2000,
    "low_liquidity_breakout_pct": 0.05,
    "low_liquidity_dead_window_hrs": 48,
    "arbitrage_max_combined_ask": 0.98
}

# Execution Safety
PAPER_TRADE_MODE = True  # If True, executioner logs trade but does NOT hit live API

# Orchestrator Scheduling (in seconds)
PIPELINE_INTERVAL_SEC = 5 * 3600       # 5 hours
OVERSEER_INTERVAL_SEC = 24 * 3600      # 24 hours

# Vault Paths (Updated to include Errors)
VAULT_PATHS = {
    "system": "Vault/00_System/",
    "filters": "Vault/01_Filters/",
    "active": "Vault/02_Active_Research/",
    "trades": "Vault/03_Trades/",
    "post_mortem": "Vault/04_Post_Mortems/",
    "errors": "Vault/05_Errors/"           # Dead letter queue for failed agents
}
