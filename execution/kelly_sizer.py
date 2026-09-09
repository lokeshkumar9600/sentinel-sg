from scipy.stats import norm

from data.config import (
    BANKROLL_USD,
    EDGE_SIGMA_RATE,
    KELLY_FRACTION,
    MAX_ASK_TO_TRADE,
    MAX_SPREAD,
    MAX_STAKE_PER_POSITION_USD,
    MAX_WIN_PCT,
    MIN_EDGE_THRESHOLD,
    MIN_LIVE_ASK,
    MIN_WIN_PCT,
)

# Risk guardrails.
MAX_SINGLE_TRADE_PCT = 0.15
MAX_TOTAL_EXPOSURE_PCT = 0.30


def compute_effective_min_edge(model_sigma: float) -> float:
    """Minimum edge to enter, scaled by model uncertainty. On a tight day
    (sigma ~0.3) this is ~1.8%, on a diffuse day (sigma ~0.9) it's ~3.2%.
    This is the main reason a 1% raw threshold still won't chase noise:
    the engine demands more edge when it's less sure about the bracket
    probability."""
    return max(MIN_EDGE_THRESHOLD, EDGE_SIGMA_RATE * model_sigma)


def _cap_to_profit_band(price: float, stake: float) -> tuple[float, float]:
    """Cap the staked amount so the max win lands inside [MIN_WIN_PCT, MAX_WIN_PCT]
    of bankroll. Returns (capped_stake, max_win_pct_of_bankroll)."""
    max_win_bankroll = stake * (1.0 - price) / price
    max_win_pct = max_win_bankroll / BANKROLL_USD if BANKROLL_USD else 0.0
    if max_win_pct > MAX_WIN_PCT:
        scale = MAX_WIN_PCT / max_win_pct
        stake *= scale
        max_win_pct = MAX_WIN_PCT
    return stake, max_win_pct


def calculate_bracket_probability(low_bound: float, high_bound: float, mu: float, sigma: float) -> float:
    return float(norm.cdf(high_bound, loc=mu, scale=sigma) - norm.cdf(low_bound, loc=mu, scale=sigma))


def _kelly_fraction(p: float, price: float) -> float:
    net_odds = (1.0 - price) / price
    return (p * net_odds - (1.0 - p)) / net_odds


def _sized_stake(side_price: float, side_prob: float) -> float | None:
    """Kelly-conviction sizing, clamped under the flat-stake ceiling and profit
    band. Quarter-Kelly computes a stake proportional to edge strength; the
    ceiling ($1) caps the downside; the profit band caps the upside. Returns
    None when the stake collapses below $0.10 (too small to be worth logging)."""
    full_kelly = _kelly_fraction(side_prob, side_price)
    if full_kelly <= 0:
        return None
    kelly_stake = min(
        BANKROLL_USD * (full_kelly * KELLY_FRACTION),
        BANKROLL_USD * MAX_SINGLE_TRADE_PCT,
    )
    stake, _ = _cap_to_profit_band(side_price, kelly_stake)
    # Clamp to the flat-stake ceiling (this is the only role of the old flat knob)
    if MAX_STAKE_PER_POSITION_USD > 0:
        stake = min(stake, MAX_STAKE_PER_POSITION_USD)
    if stake < 0.10:
        return None
    return stake


def compute_kelly_trade(
    p_model: float,
    market_price: float,
    no_price: float | None = None,
    yes_bid: float | None = None,
    no_bid: float | None = None,
    min_edge: float | None = None,
) -> dict:
    """
    Price a bracket under the model and return the action + stake.

    Changes from the old engine:
      - Spread-aware effective edge: edge is haircut by half the bid/ask spread
        so the trade is only entered if the model's probability truly beats the
        fair value after spread cost.
      - Kelly-conviction sizing under a flat ceiling: stake scales with edge
        strength (quarter-Kelly) but never exceeds MAX_STAKE_PER_POSITION_USD.
      - min_edge override: the caller can supply a per-cycle min edge from the
        confidence-scaled floor so high-sigma days demand higher edge.
    """
    edge_threshold = min_edge if min_edge is not None else MIN_EDGE_THRESHOLD

    # Liquidity sanity guard
    if market_price is None or market_price < MIN_LIVE_ASK or market_price >= MAX_ASK_TO_TRADE:
        return {
            "action": "SKIP",
            "reason": f"Thin / no liquidity: ask ${market_price if market_price is not None else 'missing'}",
            "edge": 0.0,
        }

    # --- Spread guard (YES side) ---
    yes_spread = None
    if market_price is not None and yes_bid is not None and yes_bid > 0:
        yes_spread = market_price - yes_bid
        if yes_spread > MAX_SPREAD:
            return {
                "action": "SKIP",
                "reason": f"Spread too wide: ${yes_spread:.3f} > ${MAX_SPREAD:.3f}",
                "edge": 0.0,
            }

    if no_price is None:
        no_price = 1.0 - market_price
    p_no = 1.0 - p_model
    edge_yes = p_model - market_price
    edge_no = p_no - no_price

    # Spread haircut (approx: half-spread is real fill cost)
    half_spread = max(yes_spread or 0, 0.0) / 2.0
    eff_edge_yes = edge_yes - half_spread

    # --- Spread guard (NO side) ---
    no_spread = None
    if no_price is not None and no_bid is not None and no_bid > 0 and no_price is not None:
        no_spread = no_price - no_bid
        if no_spread > MAX_SPREAD:
            return {
                "action": "SKIP",
                "reason": f"NO spread too wide: ${no_spread:.3f} > ${MAX_SPREAD:.3f}",
                "edge": 0.0,
            }
    no_half_spread = max(no_spread or 0, 0.0) / 2.0
    eff_edge_no = edge_no - no_half_spread

    # --- BUY_YES ---
    if eff_edge_yes >= edge_threshold:
        stake = _sized_stake(market_price, p_model)
        if stake is not None:
            max_win_pct = (stake * (1.0 - market_price) / market_price) / BANKROLL_USD
            return {
                "action": "BUY_YES",
                "edge": round(eff_edge_yes, 3),
                "edge_raw": round(edge_yes, 3),
                "p_model": round(p_model, 3),
                "ask_price": market_price,
                "yes_price": market_price,
                "yes_bid": yes_bid,
                "no_price": no_price,
                "stake_usd": round(stake, 2),
                "max_win_usd": round(stake * (1.0 - market_price) / market_price, 2),
                "max_win_pct": round(max_win_pct, 4),
            }

    # --- BUY_NO ---
    if eff_edge_no >= edge_threshold:
        stake = _sized_stake(no_price, p_no)
        if stake is not None:
            max_win_pct = (stake * (1.0 - no_price) / no_price) / BANKROLL_USD
            return {
                "action": "BUY_NO",
                "edge": round(eff_edge_no, 3),
                "edge_raw": round(edge_no, 3),
                "p_model": round(p_no, 3),
                "ask_price": round(no_price, 3),
                "yes_price": market_price,
                "no_price": round(no_price, 3),
                "no_bid": no_bid,
                "stake_usd": round(stake, 2),
                "max_win_usd": round(stake * (1.0 - no_price) / no_price, 2),
                "max_win_pct": round(max_win_pct, 4),
            }

    return {
        "action": "NO_TRADE",
        "edge": round(max(eff_edge_yes, eff_edge_no), 3),
        "edge_raw": round(max(edge_yes, edge_no), 3),
        "reason": f"Effective edge {max(eff_edge_yes, eff_edge_no)*100:.2f}% < {edge_threshold*100:.1f}% threshold",
    }


def size_portfolio(trades: list[dict]) -> list[dict]:
    """
    Temperature brackets are mutually exclusive outcomes of the SAME event - the true
    max temperature can only land in one of them. This scales every stake down
    proportionally so total exposure per cycle never exceeds MAX_TOTAL_EXPOSURE_PCT.
    """
    stake_trades = [t for t in trades if t["action"] in ("BUY_YES", "BUY_NO")]
    total_stake = sum(t["stake_usd"] for t in stake_trades)
    cap = BANKROLL_USD * MAX_TOTAL_EXPOSURE_PCT

    if total_stake <= cap or total_stake == 0:
        return trades

    scale = cap / total_stake
    for t in stake_trades:
        t["stake_usd"] = round(t["stake_usd"] * scale, 2)
        t["scaled_down"] = True

    return trades
