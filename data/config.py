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
MIN_EDGE_THRESHOLD = 0.08  # Require at least 8% edge to trade
KELLY_FRACTION = 0.25      # Quarter-Kelly for safety

# Per-trade profit band: size the stake so the MAXIMUM possible win of any single
# trade stays between MIN_WIN_PCT and MAX_WIN_PCT of bankroll (premium spent, not
# payout). A bracket bought at `price` for `stake` can win at most stake*(1-price)/price.
MIN_WIN_PCT = 0.02        # trades with a smaller max win than this are marginal
MAX_WIN_PCT = 0.08        # hard cap - never size a stake that could pay out >8% of bankroll

# Flat-stake override: the user trades a FIXED amount per position regardless of
# edge, time, or Kelly sizing. When >0, every entered position is sized to exactly
# this and the Kelly / profit-band sizing above is bypassed.
MAX_STAKE_PER_POSITION_USD = 1.0

# Position management (advisory book). Takes profit / stops out a live position
# when its P&L vs the entry price crosses these bands.
TAKE_PROFIT_PCT = 0.04    # close the position when it is up >= 4%
STOP_LOSS_PCT = 0.05      # close the position when it is down >= -5%

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
STORM_W_FORECAST = 0.40    # official NEA two-hour forecast says thundery for Changi area
STORM_W_METAR_TEXT = 0.25  # WSSS METAR wxString reports thunder/rain at the airport itself
STORM_W_LIGHTNING = 0.20   # live strike count across the island
STORM_W_RAIN = 0.15        # fraction of NEA stations reporting rain right now
STORM_W_RAIN_DIST = 0.15   # heavy rain proximity to Changi (suppresses peak before it arrives)