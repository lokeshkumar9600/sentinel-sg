import math
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# BUG FIX: these files live at the project root (feature_engine.py, ingestion.py),
# not in a "data" package - the original imports would raise ModuleNotFoundError.
from data.feature_engine import extract_singapore_feature_vector
from data.ingestion import fetch_all_data_gov, fetch_wsss_metar_history
from data.config import STORM_W_FORECAST, STORM_W_METAR_TEXT, STORM_W_LIGHTNING, STORM_W_RAIN, STORM_W_RAIN_DIST
from execution.polymarket import fetch_event_raw, parse_markets_from_event, get_live_clob_price, parse_temperature_bounds
from execution.kelly_sizer import calculate_bracket_probability, compute_kelly_trade, size_portfolio

SGT = ZoneInfo("Asia/Singapore")
POLL_INTERVAL_SECONDS = 60
MAX_DAYS_AHEAD_TO_CHECK = 3  # how far forward to look if today's market is resolved/missing
CLEAR_DAY_RANGE_C = 6.0      # typical Singapore clear-day dawn->peak climb (°C) driving mu's headroom


def _diurnal_heating_fraction(hr: float) -> float:
    """Fraction of the day's heating already completed, per the typical Singapore
    diurnal cycle: the daily min sits just after dawn and the max peaks ~13:00-15:00
    SGT. A smooth logistic crowded around mid-morning means the model's confidence
    that "the running max is the answer" grows continuously as the day firms up,
    instead of jumping in hard steps. Returns ~0 pre-dawn, ~1.0 from ~14:30 SGT.
    """
    if hr < 6:
        return 0.05                       # barely out of the overnight min
    if hr >= 14.5:
        return 1.0                        # past the typical peak - heating done
    return 1.0 / (1.0 + math.exp(-(hr - 10.5)))  # logistic, ~0.05 at 7:00, ~0.88 at 13:00


def _hour_based_sigma(hr: float, raw_sigma: float) -> float:
    """
    Shrink the prediction's uncertainty against WSSS's own diurnal pattern. Singapore's
    daily max almost always lands between ~12:00 and ~16:00 local; as the day moves past
    that window the odds of the running max being beaten drop fast, and by evening
    'wsss_todays_max_so_far' effectively IS the answer the market will settle against -
    regardless of whether Polymarket's API has flagged the event 'closed' yet. Rather than
    hard 12/16/19 steps, drive the taper continuously off the same heating fraction the
    mean uses: the more of the day's heating that has completed, the tighter sigma gets.
    """
    if hr < 6:
        return raw_sigma * 1.6            # pre-dawn: anchored to last night's min, genuinely uncertain
    f = _diurnal_heating_fraction(hr)
    return max(0.15, raw_sigma * (1.0 - 0.75 * f))  # ×1.0 this morning, ×0.25 once the peak is reached


def _convection_storm_score(features: dict) -> float:
    """
    Fuse all available signals into a single storm-suppression score in [0, 1]:
    how likely today's max is to be capped by convection/rain. Higher = rain will
    suppress the afternoon peak. Combines the official NEA two-hour forecast for
    the Changi area, the WSSS METAR wxString, live island lightning, live rain
    coverage, plus moisture/instability adjuvants (low cloud, falling pressure,
    near-saturated dew-point depression, and rain proximity to Changi).
    Weights live in data.config.
    """
    score = 0.0
    if features.get("changi_forecast_storm"):
        score += STORM_W_FORECAST
    if features.get("wsss_storm_txt"):
        score += STORM_W_METAR_TEXT
    lightning = features.get("lightning_strike_count", 0) or 0
    score += STORM_W_LIGHTNING * min(1.0, lightning / 10.0)
    rain = features.get("rain_station_ratio", 0.0) or 0.0
    score += STORM_W_RAIN * min(1.0, rain / 0.5)

    # NEW: spatial proximity adjuvance — heavy rain approaching the airport
    d = features.get("rain_dist_to_changi_km")
    if d is not None:
        score += STORM_W_RAIN_DIST * max(0.0, 1.0 - d / 10.0)  # within 10km adds suppression

    # Adjuvants: conditions that make convection likely even without an explicit flag.
    dpd = features.get("wsss_dpd")
    if dpd is not None and dpd < 2.5:
        score += 0.05  # near-saturated / weakly-capped air
    press_trend = features.get("wsss_press_trend_3h")
    if press_trend is not None and press_trend < -1.0:
        score += 0.05  # pressure falling -> destabilizing
    low_cloud = features.get("wsss_low_cloud_ft")
    if low_cloud is not None and 0 < low_cloud < 4000:
        score += 0.05  # low cloud = active convective development

    return min(1.0, score)


def _build_prediction_context(features: dict, storm: float, mu: float, sigma: float) -> list[str]:
    """Build a short human-readable list of why the prediction is what it is."""
    ctx = []
    # Storm components
    if features.get("changi_forecast_storm"):
        ctx.append("NEA 2hr: thundery near airport")
    if features.get("wsss_storm_txt"):
        ctx.append("WSSS METAR: thunder/rain at airport")
    lightning = features.get("lightning_strike_count", 0) or 0
    if lightning:
        ctx.append(f"{lightning} lightning strike{'s' if lightning != 1 else ''}")
    rain = features.get("rain_station_ratio", 0.0) or 0.0
    if rain:
        ctx.append(f"{rain*100:.0f}% stations raining")
    d = features.get("rain_dist_to_changi_km")
    if d is not None and d < 10:
        ctx.append(f"rain {d:.1f}km from airport")
    # Heat spread
    spread = features.get("spatial_temp_spread")
    if spread is not None:
        ctx.append(f"island spread {spread:.1f}°C")
    # Storm score summary
    if storm > 0.3:
        ctx.append(f"storm score {storm:.2f} (suppressing peak)")
    elif storm > 0.1:
        ctx.append(f"storm score {storm:.2f} (mild suppression)")
    # Diurnal
    hr = features.get("hour_of_day")
    if hr is not None:
        df = _diurnal_heating_fraction(hr)
        ctx.append(f"diurnal {df*100:.0f}% done")
    return ctx


def predict_daily_max_temp(features: dict) -> tuple[float, float]:
    """
    Predicts (mu, sigma) of today's WSSS maximum temperature.

    mu  : the running max so far PLUS the remaining clear-sky warming capacity
          (solar potential + diurnal temperature ramp extrapolated to the peak
          hour), suppressed by the fused convection/storm score - a thundery
          nowcast caps how far the temp can climb above what is already reached.
    sigma: base regression error, widened by staleness and by storm uncertainty,
          then narrowed against WSSS's own diurnal cycle as the day firms up.
    """
    current_max = features["wsss_todays_max_so_far"]
    current_temp = features["wsss_current_temp"]
    uv = features["uv_index"]
    stale_minutes = features.get("minutes_since_last_metar", 0.0)
    hr = features.get("hour_of_day", 12)

    storm = _convection_storm_score(features)

    # --- mu: remaining warming capacity on a realistic diurnal curve ---
    # How much of the day's heating is still ahead determines how far the temp can
    # climb; the residual solar potential (UV) scales that on dull/cloudy days, and
    # the short-term 3h ramp nudges the projection if the temp is rising faster (or
    # falling slower) than the climatological curve expects.
    remaining = 1.0 - _diurnal_heating_fraction(hr)
    solar = max(0.2, uv / 11.0)                       # UV 0-11; dull days add less headroom
    projected = current_temp + remaining * CLEAR_DAY_RANGE_C * solar

    ramp = features.get("wsss_temp_ramp_3h", 0.0) or 0.0
    projected += 0.15 * max(-2.0, min(2.0, ramp))     # short-term agreement bias

    headroom = max(0.0, projected - current_max) * (1.0 - storm)  # storm caps the climb
    predicted_mean = current_max + headroom            # never below the running max

    # --- sigma: uncertainty, widened by how unsure we are ---
    base_std = 0.45
    staleness_widen = min(0.6, (stale_minutes / 60.0) * 0.25)  # up to +0.6°C once data is ~2.4h old
    storm_widen = 1.2 * storm                                  # convective days are inherently harder to call
    raw_std = base_std + staleness_widen + storm_widen
    predicted_std = _hour_based_sigma(hr, raw_std)

    return predicted_mean, predicted_std


def _event_date_str(dt: datetime) -> str:
    return f"{dt.strftime('%B')}-{dt.day}-{dt.year}"


def find_live_event(max_days_ahead: int = MAX_DAYS_AHEAD_TO_CHECK):
    """
    This is what makes the target market dynamic. Starting from today (SGT), check
    each successive day's event: if it doesn't exist yet, or it's already closed/
    resolved, move on to the next day automatically. No hardcoded date, no restart
    needed when a market resolves mid-run.
    """
    now = datetime.now(SGT)
    for offset in range(max_days_ahead):
        candidate = now + timedelta(days=offset)
        date_str = _event_date_str(candidate)
        event = fetch_event_raw(date_str)

        if event is None:
            print(f"[i] No event found for {date_str} yet.")
            continue
        if event.get("closed", False):
            print(f"[i] Event for {date_str} is closed/resolved - checking next day...")
            continue

        markets = parse_markets_from_event(event)
        if markets:
            return date_str, markets
        print(f"[i] Event for {date_str} has no open brackets - checking next day...")

    return None, []


def evaluate_polymarket_brackets(event_date_str: str, mean_temp: float, std_temp: float, markets: list[dict]) -> dict:
    """
    Price Polymarket Binary Options using the predicted probability distribution,
    size each trade with the Kelly criterion, then apply a portfolio-level cap
    across brackets (they're mutually exclusive outcomes of the same event).

    Returns the full structured result so HTTP callers (server.py) can serialize
    it; the terminal output below is unchanged for the CLI loop.
    """
    print(f"\n--- LIVE PREDICTION for {event_date_str} ({datetime.now(SGT).strftime('%H:%M:%S SGT')}) ---")
    print(f"Predicted Max Temp (WSSS): {mean_temp:.2f}°C (±{std_temp:.2f}°C)\n")

    trades = []
    for m in markets:
        title = m["group_item_title"] or m["question"]
        low, high = parse_temperature_bounds(title)
        prob = calculate_bracket_probability(low, high, mean_temp, std_temp)
        # Use Gamma's bestAsk (accurate for negRisk brackets); fall back to the
        # CLOB book if the metadata is missing it.
        price = m.get("best_ask")
        if price is None:
            price = get_live_clob_price(m["yes_token_id"])
        # negRisk NO price derived from the bracket group (sum of other YES asks).
        no_price = m.get("no_price")
        trade = compute_kelly_trade(prob, price, no_price=no_price)
        trade["bracket"] = title
        trade["prob"] = prob
        trade["price"] = price
        trade["yes_price"] = trade.get("yes_price", price)
        trade["no_price"] = trade.get("no_price", no_price)
        # Buy/sell reference prices for BOTH sides (frontend shows all four);
        # buy = ask, sell = bid.  None when the market is too thin to quote.
        trade["yes_sell"] = m.get("best_bid")
        trade["no_sell"] = m.get("no_bid")
        trades.append(trade)

    trades = size_portfolio(trades)

    print(f"{'Bracket':<20} | {'Model Prob':<10} | {'Ask':<8} | {'Action':<9} | {'Stake':<10}")
    print("-" * 70)
    for t in trades:
        stake_str = f"${t['stake_usd']:.2f}" if "stake_usd" in t else "-"
        if t.get("scaled_down"):
            stake_str += " (capped)"
        print(f"{t['bracket']:<20} | {t['prob']*100:8.1f}% | {t['price']:6.2f} | {t['action']:<9} | {stake_str}")

    return {
        "event_date_str": event_date_str,
        "mean_c": round(mean_temp, 2),
        "std_c": round(std_temp, 2),
        "generated_at_sgt": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT"),
        "trades": trades,
    }


def main_loop():
    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        print("[+] Ingesting from data.gov.sg real-time APIs + WSSS METAR...")

        # 1. Pipeline Ingestion
        raw_gov_data = fetch_all_data_gov()
        wsss_history = fetch_wsss_metar_history()

        # 2. Vector Extraction
        features = extract_singapore_feature_vector(raw_gov_data, wsss_history)

        # 3. Model Inference (risk-adjusted)
        mu, sigma = predict_daily_max_temp(features)

        # 4. Find the currently-live event (auto-rolls forward if today's has resolved)
        print("[+] Locating live Polymarket event...")
        event_date_str, markets = find_live_event()

        # 5. Evaluate
        if markets:
            evaluate_polymarket_brackets(event_date_str, mu, sigma, markets)
        else:
            print(f"[!] No live event found within the next {MAX_DAYS_AHEAD_TO_CHECK} days. Retrying next cycle...")

        print(f"\n[~] Sleeping for {POLL_INTERVAL_SECONDS} seconds before next API pull...")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        print("\n[!] Exiting continuous evaluation loop.")