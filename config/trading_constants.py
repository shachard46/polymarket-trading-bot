"""
Configuration constants for the Polymarket Trading Pipeline.
"""
# Allocation & Math Constants (see docs/03_strategy_math.md)
TAX = 0.25                 # Israeli capital-gains rate
# Tax is 25% of NET capital gains per calendar year (losses offset gains within the year);
# trade maths is pre-tax.
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
RESEARCH_MAX_QUICK_WIDTH = 0.20     # quick range wider than this is "uninformative" -> escalate (also escalates on confidence "low")
RESEARCH_MAX_SAMPLE_SPREAD = 0.15   # max(p) - min(p) across samples above this -> sample_disagreement
RESEARCH_MAX_Q_GAP = 0.40           # |p_median - q| above this -> probable_misread (paper-trading: wider)
RESEARCH_THIN_EVIDENCE_MAX_GAP = 0.15  # an unanswered "evidence" crux plus |p - q| above this -> thin_evidence_disagreement (no trade)
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
    "concentrated_top1_supply": 0.25,         # same, basis = share of outstanding shares (data-api /v2/oi)
    "concentrated_top3_supply": 0.50,
    "wide_spread_max": 0.08,                  # yes_ask - yes_bid above this -> wide_spread
    "holders_limit": 20,                      # data-api /holders top-N
    "holders_timeout_sec": 5.0,
}

# Quick-stage shadow test (Phase 3, measure only): a stable ~10% of quick no_divergence skips run
# the FULL stage anyway; results are ledgered (stage shadow_research) and never traded.
SHADOW_RATE = 0.10
SHADOW_MAX_PER_TICK = 2
# Triage shadow: a stable ~10% of triage rejects get a measure-only quick-stage estimate (ledger
# stage triage_shadow) so we can score what permanent triage rejects cost us.
TRIAGE_SHADOW_RATE = 0.10
TRIAGE_SHADOW_MAX_PER_TICK = 2
# Signal coverage: log an error when more than this share of gated markets lack a signal.
SIGNAL_COVERAGE_ALERT_FRAC = 0.5
# Open failure tags: a post-mortem 'new:<slug>' tag is shown to the Overseer as a class once it
# has been proposed at least this many times.
FAILURE_TAG_MIN_COUNT = 3

# Market integrity (Phase 2, deterministic, record-first): features over 1d/7d/30d from CLOB price
# history, data-api v2 trades and the CLOB book, only for markets inside the gate budget. Hard
# rejects only for extreme cases; everything else is ledgered with a 0-1 integrity score.
# Fixed constants, NOT Overseer knobs. Missing data never rejects.
INTEGRITY = {
    # fetch
    'history_days': 30,
    'trades_limit': 1000,
    'timeout_sec': 5.0,
    # hard rejects
    'ramp_thin_min_move': 0.25,            # |p_now - p_30d| at/above this ...
    'ramp_thin_min_vol_usd': 5000.0,       # ... with 30d volume below max(this, liquidity) -> ramp_thin
    'wallet_dom_top1_share': 0.60,         # top-1 wallet share of 30d volume at/above this ...
    'wallet_dom_min_trades': 10,           # ... with at least this many 30d trades -> wallet_dominated
    'few_wallets_max': 4,                  # n_wallets_30d <= this ...
    'few_wallets_min_move': 0.15,          # ... with ramp_30d >= this -> wallet_dominated
    'disputes_reject_min': 1,              # n 'disputed' in umaResolutionStatuses -> disputed_before
    # soft-flag alarm levels (flag = clip(value / alarm, 0, 1); score = 1 - mean(flags))
    'alarm_ramp_30d': 0.30,
    'alarm_top1_share': 0.60,
    'alarm_two_sided_share': 0.50,
    'alarm_days_since_trade': 7.0,
    'alarm_vol_spike': 5.0,
    'alarm_liq_to_depth': 10.0,
    'alarm_ask_wall_share': 0.80,
    'young_age_days': 7.0,                 # flag = clip(1 - age_days / this, 0, 1)
}

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
    "research_thin_evidence_max_gap": RESEARCH_THIN_EVIDENCE_MAX_GAP,  # read-only default: not an Overseer knob (not in BOUNDS)
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
# Directive TEXT guard (fixed group, NOT Overseer knobs, not in BOUNDS). The knobs are bounded; the free text of
# the Research Protocol / Risk Constraints / Output Requirements sections goes into every deep_researcher call, so
# the Hub checks it deterministically and rejects the WHOLE version on any violation.
DIRECTIVE_TEXT_SECTIONS = ("Research Protocol", "Risk Constraints", "Output Requirements")
DIRECTIVE_TEXT_MAX_CHARS = 4000          # max length of one text section
DIRECTIVE_TEXT_MIN_SIMILARITY = 0.6      # difflib ratio vs the current section: below this = rewrite, rejected
DIRECTIVE_TEXT_RATIO_MIN_LEN = 200       # the ratio check applies only to new sections longer than this
DIRECTIVE_TEXT_FORBIDDEN_PATTERNS = (    # (reason, case-insensitive regex)
    ("url", r"https?://|\bwww\.|\bftp://"),
    ("market_talk", r"polymarket|kalshi|predictit|manifold|metaculus|market[\s_-]+price|implied[\s_-]+probabilit|\bodds\b"),
    ("instruction_override", r"ignore\s+(?:all|any|the|previous|prior|above|earlier)(?:\s+\w+){0,2}\s+(?:instructions?|rules|prompts?)"
                             r"|system\s+prompt|you\s+are\s+now|disregard|new\s+instructions"),
)
AUTO_REVERT_MIN_SCORED = 30         # resolved scored markets a version needs before it is judged
AUTO_REVERT_BRIER_WORSE_BY = 0.02   # current Brier worse than previous by more than this -> revert

# Concentration (Phase 3 queue): FULL research per tick is capped per event and per triage country.
MAX_FULL_PER_EVENT_PER_TICK = 1     # one FULL research per event_id per tick (siblings are deferred, not failed)
MAX_FULL_PER_COUNTRY_PER_TICK = 2   # per triage country (ISO-2); a missing or "global" country is uncapped
# Event-sibling guard (Phase 4): look-back window and the sum of researched p across mutually exclusive siblings.
EVENT_SIBLING_WINDOW_DAYS = 3
EVENT_SIBLING_MAX_P_SUM = 1.15

# Question-led FULL stage (crux_v1): scout -> architect -> per-crux search -> one gap round -> audit -> 2 judges.
RESEARCH_PIPELINE = "crux_conf_v1"  # crux_v1 + recorded confidence signals (code level drives sizing)
RESEARCH_MAX_WEB_CALLS = 12         # search calls per market (scout + crux queries + gap follow-ups)
RESEARCH_LOCAL_EXTRA_WEB_CALLS = 6  # extra search calls per market, only when the market locale is non-English (local-language queries)
LOCAL_QUERY_ROLES = ("evidence", "comparator", "horizon")  # cruxes that may carry one local-language query
MAX_GAP_ROUNDS = 1                  # gap-analyzer rounds (capped in code)
GAP_MAX_QUERIES = 3                 # follow-up queries one gap round may add
AUDIT_MAX_LINES = 10                # evidence lines the auditor checks per market
CRUX_COUNT = {"full": (5, 8), "lite": (4, 5)}  # (min, max) cruxes per depth
CRUX_MAX_QUERIES = 2                # queries per crux
CRUX_SEARCH_WORKERS = 4             # crux searches run in parallel
DEPTH_FULL_MIN_USD = 25.0           # potential max order below this -> RESEARCH_DEPTH "lite" (fewer cruxes, no audit)
JUDGE_PARTIAL_LINES = 2               # judge input: lines sent for a still-unanswered crux (labelled partial)
JUDGE_LINES_PER_CRUX = 4              # judge input: top evidence lines kept per answered/contradicted crux (quality tier, then recency)
COMPOUND_MAX_CORR = 0.9             # judge-assumed correlation between compound components is clamped to [0, this]
COMPOUND_MAX_COMPONENTS = 4         # components of a compound market (AND / OR); more are ignored
SOURCE_CLASSIFIER_MAX_DOMAINS = 40  # unknown domains sent to the source_classifier per research
JUDGE_ARM_MODES = ("ab", "one", "two")  # JUDGE_ARM_MODE: stable per-market A/B, always one judge, always two judges
EVIDENCE_QUALITY_TIERS = ("official", "major", "aggregator", "blog", "unknown")

# Evidence quality: age labels on the bundle the deep researcher sees, and the Brave web backend.
EVIDENCE_STALE_DAYS = 180           # bundle lines older than this are tagged STALE
BRAVE_RESULTS_PER_QUERY = 10        # Brave Web Search results requested per query
BRAVE_FETCH_PAGES = 5               # top results whose pages are fetched and read per query
BRAVE_SEARCH_TIMEOUT_SEC = 15       # per Brave API request
BRAVE_PAGE_TIMEOUT_SEC = 8          # per page fetch
BRAVE_PAGE_MAX_BYTES = 500_000      # download cap per page (the rest is ignored)
BRAVE_PAGE_TEXT_CHARS = 6000        # extracted text per page sent to the extractor
BRAVE_QUERY_WORKERS = 4             # queries searched in parallel
BRAVE_MAX_RETRIES = 3               # retries of one Brave request after HTTP 429
BRAVE_BACKOFF_CAP_SEC = 10.0        # longest single wait after a 429
BRAVE_USER_AGENT = "pmbot-research/1.0"
GEMINI_QUERY_WORKERS = 4            # gemini backend: one web_searcher call per query, this many in parallel

# Confidence (conf_v1): the code-scored level is a property of the evidence (never of q); it drives sizing.
# Rule v1 in orchestrator/confidence.py. Three signals are recorded on every researched market and compared by Brier.
SIZING_CONFIDENCE_SIGNAL = "code_level"   # the one-line switch: "code_level" | "judge_score" | "width"
CONFIDENCE_WEIGHTS = {"high": 1.0, "medium": 0.5, "low": 0.0}   # shrink = lambda * weight(level); low -> no trade
JUDGE_SCORE_LEVELS = ((8, "high"), (4, "medium"), (1, "low"))   # judge 1-10 score -> level (min score, level)
CONFIDENCE_LOW_SPREAD_MIN = 0.10          # two-judge spread above this -> low
CONFIDENCE_LOW_OFFICIAL_SHARE_MAX = 0.20  # official+major share of source_mix below this -> low
CONFIDENCE_HIGH_OFFICIAL_SHARE_MIN = 0.40 # high needs official+major share at least this
CONFIDENCE_HIGH_SPREAD_MAX = 0.05         # high needs one judge or spread at most this
CONFIDENCE_HIGH_UNANSWERED_MAX = 1        # high needs at most this many unanswered cruxes (and 0 evidence ones)
CONFIDENCE_HIGH_BASE_RATE_N_MIN = 10      # reference_class prior needs base_rate_n at least this for high
CONFIDENCE_HIGH_AUDIT_DROPPED_MAX = 2     # high needs at most this many audit-dropped lines
# Record-only comparison buckets for stats (judge score, range width)
CONFIDENCE_WIDTH_BUCKETS = (0.20, 0.35)   # width < 0.20 / 0.20-0.35 / > 0.35

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
    "concierge": "Vault/06_Concierge/",
}

# In-place pipeline state keys (filter frontmatter / trade JSON / research frontmatter)
STATUS_KEY = "status"
ERROR_LOG_KEY = "error_log"
STATUS_INACTIVE = "inactive"
BELOW_EDGE_KEY = "below_edge_threshold"
