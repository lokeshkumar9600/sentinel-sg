import os
from dotenv import load_dotenv

load_dotenv()

DATA_GOV_API_KEY = os.getenv("DATA_GOV_API_KEY", "")
WSSS_ICAO = "WSSS"

# Polymarket API Endpoints
GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"

# Risk & Trading Controls
BANKROLL_USD = 1000.0
# Edge target: the model only enters when its edge over the market's ask is
# >= this. You asked to trade a 1% target, so this is 1% - but the effective bar
# is higher in practice because (a) the spread haircut below eats most 1-3% edges
# on a real book, and (b) the confidence-scaled floor raises it on uncertain days.
MIN_EDGE_THRESHOLD = 0.01
KELLY_FRACTION = 0.25      # Quarter-Kelly for safety

# Per-trade profit band: size the stake so the MAXIMUM possible win of any single
# trade stays between MIN_WIN_PCT and MAX_WIN_PCT of bankroll (premium spent, not
# payout). A bracket bought at `price` for `stake` can win at most stake*(1-price)/price.
MIN_WIN_PCT = 0.005       # trades whose max win is smaller than this are flagged
MAX_WIN_PCT = 0.01        # hard cap: a single trade may pay out at most 1% of bankroll ($10)

# Stake ceiling per position. Previously a FIXED stake; now a per-position CAP: the
# engine sizes by quarter-Kelly under the profit band, then clamps to this. It can
# never risk more than $1 per position, so a bad bracket loses at most ~$1.
MAX_STAKE_PER_POSITION_USD = 1.0

# Position management (advisory book). Takes profit / stops out a live position
# when its P&L vs the entry price crosses these bands.
TAKE_PROFIT_PCT = 0.01    # close the position when it is up >= 1% (your target)
STOP_LOSS_PCT = 0.02      # close the position when it is down >= -2% (defense-first)
# NOTE: the P&L stop cannot catch a bid that gaps down in one tick - see
# FAIR_VALUE_EXIT_RATIO below for the model-driven exit that CAN.

# Anti-churn cooldown: after a bracket+side is STOPped, refuse to re-enter
# that same bracket+side for this many seconds. Prevents the engine from
# re-asserting the same stale false edge every 30 minutes (which drove
# 16 trades on Sept 5 with old sigma).  60 min = 3600 s.
STOP_COOLDOWN_SECONDS = 3600

# Model-driven guard rail: exit a losing position the moment the model's live fair
# value for the held side drops to <= entry_price * FAIR_VALUE_EXIT_RATIO. Because
# the model re-rates BEFORE the market finishes repricing, this closes at a bid
# that still exists instead of the post-collapse ~$0.00. 1.0 = exit at break-even.
FAIR_VALUE_EXIT_RATIO = 0.75

# Daily loss circuit breaker: after realized losses for the SGT day exceed this
# share of bankroll, no new entries are staged until the next day.
DAILY_LOSS_LIMIT_PCT = 0.03   # halt entries for the day after -$30 on a $1,000 bankroll

# Spread guard: refuse to enter a bracket whose bid/ask spread is wider than this.
# A 1% "edge" on a 6-cent-wide book is 1% - 3c = worthless; only real, liquid
# books get traded.
MAX_SPREAD = 0.03

# Confidence-scaled minimum edge: on diffuse days (large model sigma) the minimum
# edge rises so the engine stops chasing its own uncertainty. Floor at 1%, scaled
# linearly by sigma in "probability" units (1/40 per °C of model sigma).
EDGE_SIGMA_RATE = 0.025

# When-to-trade timing gate (SGT). Prefer entering during this window once the
# model's mu has formed but the market hasn't fully repriced; near-final "lockout"
# (diurnal heating ~1) can still enter if a durable edge remains.
ENTRY_WINDOW_HOURS = (10, 15)

# If ANY of these NEA areas are under a thundery/rainy two-hour forecast, the
# model treats today as storm-suppressed (temp unlikely to climb far above what
# has already been reached). Primarily the towns around WSSS/Changi.
TWO_HR_CHANGI_AREAS = ("Changi", "Pasir Ris", "Tampines", "Bedok", "Paya Lebar")

# Convection-suppression weights (storm score contributions), kept tunable here
# so the "how scared is the model of rain" question is a config change, not a
# code change. Sum to <= 1.
STORM_W_FORECAST = 0.35    # official NEA two-hour forecast says thundery for Changi area
STORM_W_METAR_TEXT = 0.25  # WSSS METAR wxString reports thunder/rain at the airport itself
STORM_W_LIGHTNING = 0.15   # live strike count across the island
STORM_W_RAIN = 0.15        # fraction of NEA stations reporting rain right now
STORM_W_RAIN_DIST = 0.10   # heavy rain proximity to Changi (suppresses peak before it arrives)

# Climatological prior for daily max temperature (Singapore)
CLIM_MEAN_DEFAULT = 31.3    # September mean, °C (constant for now; sinusoid deferred)
CLIM_SIGMA = 0.9            # climatological sigma floor, °C (used when live signal is weak)

# Irreducible residual uncertainty of the daily-max forecast (data/calibration.py).
# The historical WSSS METAR replay shows the model's diurnal sigma taper shrinks
# to ~0.15°C by mid-afternoon precisely when the max is still climbing — a 3-6x
# overstatement of confidence (empirical |error| runs 0.3-1.4°C there; 1σ coverage
# was 36% pooled vs the 68% a calibrated model should hit). This residual is
# combined in quadrature with the taper so the claimed uncertainty never
# collapses below the irreducible day-to-day forecast spread.
SIGMA_RESIDUAL_FLOOR_C = 0.5

# Daily self-improvement (see data/model_learner.py): the model learns a
# bias-correction and a climatology mean from the settled prediction journal.
LEARN_MIN_SAMPLES = 3       # settled days required before tuning engages
LEARN_BIAS_CLAMP = 0.5      # max °C the learned bias may shift the forecast

# Morning systematic under-prediction correction (data/calibration.py, 8-day
# WSSS METAR replay). The model's mu runs ~+1.0°C low during mid-morning
# (H8-H12): the diurnal heating curve + NEA-high prior systematically understate
# how much the day still climbs before peaking. This is a reproducible bias —
# 7/8 replayed days under-predicted at every hour 8-12 — not noise. Per-hour
# offsets (H8/9 -> +1.0, H10/11 -> +0.7, H12 -> +0.3, H13+ -> 0) lift mu toward
# the observed daily max; sigma is left untouched so bracket coverage improves
# (0.67 -> 0.83 pooled, MAE 0.648 -> 0.440) without manufacturing confidence.
MORNING_BIAS_HOURS = {8: 1.0, 9: 1.0, 10: 0.7, 11: 0.7, 12: 0.3}

# Signal gate: hours during which the model produces live trading signals
TRADEABLE_HOURS_START = 8   # 08:00 SGT
TRADEABLE_HOURS_END = 20    # 20:00 SGT

# Price sanity / liquidity guards
MIN_LIVE_ASK = 0.03         # refuse asks below $0.03 (a 1-2c ask is thin noise, not edge)
MAX_ASK_TO_TRADE = 0.97     # refuse asks at/above $0.97 (near-certain, no edge)