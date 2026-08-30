from scipy.stats import norm

from data.config import (
    BANKROLL_USD,
    KELLY_FRACTION,
    MAX_STAKE_PER_POSITION_USD,
    MAX_WIN_PCT,
    MIN_EDGE_THRESHOLD,
    MIN_WIN_PCT,
)

# Risk guardrails. These are deliberately conservative defaults - move them into
# config.py if you want them tunable without a code change.
MAX_SINGLE_TRADE_PCT = 0.15   # no single bracket gets more than 15% of bankroll
MAX_TOTAL_EXPOSURE_PCT = 0.30  # all brackets combined, capped at 30% of bankroll per cycle

# Profit band: size the stake so the MAXIMUM possible payout of any single
# trade (stake * (1-price)/price) stays within [MIN_WIN_PCT, MAX_WIN_PCT] of
# bankroll. So stake <= BANKROLL * MAX_WIN_PCT * price / (1-price).


def _cap_to_profit_band(price: float, stake: float) -> tuple[float, float]:
    """Cap the staked amount so the max win lands inside [MIN_WIN_PCT, MAX_WIN_PCT]
    of bankroll. Returns (capped_stake, max_win_pct_of_bankroll)."""
    max_win_bankroll = stake * (1.0 - price) / price
    max_win_pct = max_win_bankroll / BANKROLL_USD if BANKROLL_USD else 0.0
    min_pct, max_pct = MIN_WIN_PCT, MAX_WIN_PCT
    # Hard cap at 8%
    if max_win_pct > max_pct:
        scale = max_pct / max_win_pct
        stake *= scale
        max_win_pct = max_pct
    # Below 2% — flag but don't block (could be deep-stack trash; the caller decides)
    return stake, max_win_pct


def calculate_bracket_probability(low_bound: float, high_bound: float, mu: float, sigma: float) -> float:
    return float(norm.cdf(high_bound, loc=mu, scale=sigma) - norm.cdf(low_bound, loc=mu, scale=sigma))


def _kelly_fraction(p: float, price: float) -> float:
    net_odds = (1.0 - price) / price
    return (p * net_odds - (1.0 - p)) / net_odds


def compute_kelly_trade(p_model: float, market_price: float, no_price: float | None = None) -> dict:
    if market_price <= 0 or market_price >= 1.0:
        return {"action": "SKIP", "reason": "No Liquidity / Market Closed"}

    edge_yes = p_model - market_price

    # ENHANCEMENT / BUG FIX: the original only ever evaluated buying YES. On a binary
    # market that leaves real edge on the table - if the model thinks YES is overpriced,
    # there's an equal-and-opposite edge on buying NO, which was never checked.
    # For Polymarket negRisk bracket groups there is no standalone NO book: buying NO
    # means buying YES on all the OTHER brackets, so its price is passed in explicitly.
    # On a plain binary market no_price defaults to the fair 1 - yes.
    if no_price is None:
        no_price = 1.0 - market_price
    p_no = 1.0 - p_model
    edge_no = p_no - no_price

    if edge_yes >= MIN_EDGE_THRESHOLD:
        full_kelly = _kelly_fraction(p_model, market_price)
        if full_kelly > 0:
            if MAX_STAKE_PER_POSITION_USD > 0:
                # Flat-stake mode: ignore Kelly, fix the stake at the user's amount.
                stake = MAX_STAKE_PER_POSITION_USD
            else:
                stake = min(
                    BANKROLL_USD * (full_kelly * KELLY_FRACTION),
                    BANKROLL_USD * MAX_SINGLE_TRADE_PCT,  # hard cap: a single mispriced bracket
                )                                          # (e.g. a bad probability estimate)
                stake, _ = _cap_to_profit_band(market_price, stake)
            max_win_pct = (stake * (1.0 - market_price) / market_price) / BANKROLL_USD
            return {
                "action": "BUY_YES",
                "edge": round(edge_yes, 3),
                "p_model": round(p_model, 3),
                "ask_price": market_price,
                "yes_price": market_price,
                "no_price": no_price,
                "stake_usd": round(stake, 2),
                "max_win_usd": round(stake * (1.0 - market_price) / market_price, 2),
                "max_win_pct": round(max_win_pct, 4),
            }

    if edge_no >= MIN_EDGE_THRESHOLD:
        full_kelly_no = _kelly_fraction(p_no, no_price)
        if full_kelly_no > 0:
            if MAX_STAKE_PER_POSITION_USD > 0:
                stake = MAX_STAKE_PER_POSITION_USD
            else:
                stake = min(
                    BANKROLL_USD * (full_kelly_no * KELLY_FRACTION),
                    BANKROLL_USD * MAX_SINGLE_TRADE_PCT,
                )
                stake, _ = _cap_to_profit_band(no_price, stake)
            max_win_pct = (stake * (1.0 - no_price) / no_price) / BANKROLL_USD
            return {
                "action": "BUY_NO",
                "edge": round(edge_no, 3),
                "p_model": round(p_no, 3),
                "ask_price": round(no_price, 3),
                "yes_price": market_price,
                "no_price": round(no_price, 3),
                "stake_usd": round(stake, 2),
                "max_win_usd": round(stake * (1.0 - no_price) / no_price, 2),
                "max_win_pct": round(max_win_pct, 4),
            }

    return {"action": "NO_TRADE", "edge": round(edge_yes, 3), "reason": f"Edge < {MIN_EDGE_THRESHOLD*100}%"}


def size_portfolio(trades: list[dict]) -> list[dict]:
    """
    Temperature brackets are mutually exclusive outcomes of the SAME event - the true
    max temperature can only land in one of them. compute_kelly_trade sizes each bracket
    independently, so if several brackets show edge in the same cycle (plausible when your
    model's mu/sigma disagrees with the market's implied distribution across multiple
    buckets), the stakes can sum to well over 100% of bankroll. This scales every stake
    down proportionally so total exposure per cycle never exceeds MAX_TOTAL_EXPOSURE_PCT.
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