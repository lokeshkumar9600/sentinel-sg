import math
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# BUG FIX: these files live at the project root (feature_engine.py, ingestion.py),
# not in a "data" package - the original imports would raise ModuleNotFoundError.
from data.feature_engine import extract_singapore_feature_vector
from data.ingestion import fetch_all_data_gov, fetch_wsss_metar_history
from data.config import STORM_W_FORECAST, STORM_W_METAR_TEXT, STORM_W_LIGHTNING, STORM_W_RAIN, STORM_W_RAIN_DIST, CLIM_MEAN_DEFAULT, CLIM_SIGMA, SIGMA_RESIDUAL_FLOOR_C, TRADEABLE_HOURS_START, TRADEABLE_HOURS_END, MIN_LIVE_ASK, MAX_ASK_TO_TRADE, MORNING_BIAS_HOURS
from execution.polymarket import fetch_event_raw, parse_markets_from_event, get_live_clob_price, parse_temperature_bounds
from execution.kelly_sizer import calculate_bracket_probability, compute_effective_min_edge, compute_kelly_trade, size_portfolio
from data.storm_timing import compute_storm_timing_factor, refine_storm_score

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
    regardless of whether Polymarket's API has flagged the event 'closed' yet.

    The shrink rate is moderate (0.6 × diurnal fraction, not 0.75) so sigma stays
    meaningfully above zero during mid-morning — when the model still relies heavily
    on the NEA forecast and has limited live observations.  The floor (0.45°C) is
    Singapore's inherent daily-max variability: even late in the day there's genuine
    uncertainty about whether another reading will edge above the running max.

    The irreducible residual uncertainty is combined in quadrature so sigma never
    collapses below sqrt(0.45² + 0.5²) ≈ 0.67°C. This fixes the calibration defect
    where sigma hits 0.45 at hours 12-13 while empirical errors run 1+°C.
    """
    if hr < 6:
        return raw_sigma * 1.9            # pre-dawn: anchored to last night's min, genuinely uncertain
    f = _diurnal_heating_fraction(hr)
    # Diurnal taper component (same as before: slower shrinkage, 0.45 floor)
    taper = max(0.45, raw_sigma * (1.0 - 0.6 * f))
    # Combine in quadrature with the irreducible residual floor
    return (taper**2 + SIGMA_RESIDUAL_FLOOR_C**2) ** 0.5


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
        ctx.append(f"{rain*100:.2f}% stations raining")
    d = features.get("rain_dist_to_changi_km")
    if d is not None and d < 10:
        ctx.append(f"rain {d:.2f}km from airport")
    # Heat spread
    spread = features.get("spatial_temp_spread")
    if spread is not None:
        ctx.append(f"island spread {spread:.2f}°C")
    # Storm score summary
    if storm > 0.3:
        ctx.append(f"storm score {storm:.2f} (suppressing peak)")
    elif storm > 0.1:
        ctx.append(f"storm score {storm:.2f} (mild suppression)")
    # Storm timing
    if timing:
        ctx.append(f"storm age_pen={timing.get('storm_age_penalty', 1.0):.2f} time_pen={timing.get('time_of_day_penalty', 1.0):.2f}")
    # Trend features
    yest = features.get("yesterday_max_temp")
    three = features.get("three_day_avg_max")
    if yest is not None and three is not None:
        ctx.append(f"trend: yest={yest:.1f} 3d_avg={three:.1f} dev={yest-three:.1f}")
    delta_y = features.get("temp_delta_yesterday")
    if delta_y is not None:
        ctx.append(f"delta_vs_yest={delta_y:.1f}")
    # Diurnal
    hr = features.get("hour_of_day")
    if hr is not None:
        df = _diurnal_heating_fraction(hr)
        ctx.append(f"diurnal {df*100:.2f}% done")
    # NEA forecast prior
    fhi = features.get("nea_forecast_high")
    flo = features.get("nea_forecast_low")
    if fhi is not None:
        ctx.append(f"NEA forecast {flo if flo is not None else '?'}–{fhi:.1f}°C (prior)")
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

    # --- Trend-aware forecasting using lag/rolling features ---
    # These features provide day-over-day context that the physics model alone misses.
    yesterday_max = features.get("yesterday_max_temp")
    three_day_avg = features.get("three_day_avg_max")
    temp_delta_yesterday = features.get("temp_delta_yesterday")

    storm = _convection_storm_score(features)

    # Apply temporal decay and diurnal convective cycle scaling to the storm score
    timing = compute_storm_timing_factor(features)
    storm = refine_storm_score(storm, timing)

    # --- mu: remaining warming capacity on a realistic diurnal curve ---
    # How much of the day's heating is still ahead determines how far the temp can
    # climb; the residual solar potential (UV) scales that on dull/cloudy days, and
    # the short-term 3h ramp nudges the projection if the temp is rising faster (or
    # falling slower) than the climatological curve expects.
    remaining = 1.0 - _diurnal_heating_fraction(hr)
    solar = max(0.2, uv / 11.0)                       # UV 0-11; dull days add less headroom
    # Adaptive clear-day climb: use the NEA 24h forecast's expected high-low span
    # (clamped to a sane 5-8°C band) when available, else the fixed 6.0°C constant.
    # A hot day (wide range) expects more heating; a cool/rainy day expects less.
    nea_high = features.get("nea_forecast_high")
    nea_range = features.get("nea_day_range")
    day_range = max(5.0, min(8.0, nea_range)) if nea_range is not None else CLEAR_DAY_RANGE_C
    projected = current_temp + remaining * day_range * solar

    ramp = features.get("wsss_temp_ramp_3h", 0.0) or 0.0
    projected += 0.15 * max(-2.0, min(2.0, ramp))     # short-term agreement bias

    # Trend adjustment: if yesterday was hotter/cooler than the 3-day average,
    # nudge the projection in that direction (momentum effect, bounded).
    if yesterday_max is not None and three_day_avg is not None:
        # yesterday deviation from 3-day trend (positive = yesterday hotter)
        trend_dev = yesterday_max - three_day_avg
        # Current temp deviation from yesterday's max (positive = already warmer)
        delta_yest = temp_delta_yesterday if temp_delta_yesterday is not None else 0.0
        # Combined trend signal: blend yesterday's trend with today's early delta
        # Weight: 60% yesterday's anomaly, 40% today's early delta (when available)
        trend_signal = 0.6 * trend_dev + 0.4 * delta_yest
        # Bound the adjustment to ±0.8°C to avoid over-correction on outliers
        trend_adjustment = max(-0.8, min(0.8, 0.3 * trend_signal))
        projected += trend_adjustment

    headroom = max(0.0, projected - current_max) * (1.0 - storm)  # storm caps the climb
    predicted_mean = current_max + headroom            # never below the running max

    # --- sigma: uncertainty, widened by how unsure we are ---
    # base_std: the model's irreducible prediction error on a clear day with fresh
    # data. 0.65 reflects that even with a perfect NEA forecast and live METAR,
    # Singapore's daily max has ~0.5-0.7°C of genuine day-to-day variability
    # around the forecast that no model can eliminate.  (The old 0.45 value was
    # too low — it let the model claim 51% on a single bracket at 10am, creating
    # false edges over thin-market asks that stopped out within one minute.)
    base_std = 0.65
    staleness_widen = min(0.6, (stale_minutes / 60.0) * 0.25)  # up to +0.6°C once data is ~2.4h old
    storm_widen = 1.2 * storm                                  # convective days are inherently harder to call
    raw_std = base_std + staleness_widen + storm_widen

    # Forecast-aware uncertainty expansion: when the NEA forecast high sits well
    # above the running max (large warming gap), the model's projection is driven
    # by the forecast's accuracy, not live observations.  Expanding sigma by the
    # forecast gap makes the model appropriately uncertain about tail-bracket bets
    # during the early-morning "forecast-only" regime, reducing false edges that
    # stop out within one polling cycle.
    fc_gap = max(0.0, (nea_high or current_max) - current_max)
    forecast_expand = 0.08 * min(6.0, fc_gap)  # +0.48°C for a 6°C gap, 0 when gap is zero
    predicted_std = _hour_based_sigma(hr, raw_std + forecast_expand)

    # --- Prior blend (overnight/early-morning accuracy) ---
    # Blend the live projection with a daily-max prior. Weight w tracks the
    # diurnal heating fraction: low early (trust the prior), 1.0 by afternoon.
    df = _diurnal_heating_fraction(hr)
    w = 0.15 + 0.85 * df                    # overnight (df~0.05) -> w~0.19; midday -> w~1.0

    # --- Daily self-improvement (meta-learning) ---
    # Fit a learned climatology + bias-correction + per-hour bias curve from the
    # settled journal.  Once enough days have settled (LEARN_MIN_SAMPLES), the
    # climatological prior is the site's real observed mean.  Before that, NEA's
    # official 24h forecast high — the meteorologists' own expected daily max —
    # replaces the fixed September constant, so the overnight prediction respects
    # the expert forecast.
    from data.model_learner import self_tune
    tune = self_tune()

    # Data-driven climatology: use the ACTUAL observed WSSS daily max history
    # for this month (learned, temp LOOKUP as of 2026) in preference to the
    # hardcoded CLIM_MEAN_DEFAULT (31.3 understated September by ~1.5°C), then
    # the learned journal climatology, then the NEA forecast, then the default.
    # The climatology module is cheap (reads a local cache) so it's safe on the
    # hot prediction path.
    try:
        from data.climatology import climatology_for_month
        climo = climatology_for_month()
        clim_mean = float(climo["mean"])
        clim_std = float(climo["std"])
        climo_source = climo["source"]
    except Exception:  # noqa: BLE001 — never let climatology break the forecast
        clim_mean, clim_std, climo_source = None, None, "unavailable"

    if clim_mean is not None and 27.0 <= clim_mean <= 37.0:
        # Climatology prior from real data outranks everything.  Includes a
        # slight drift-tolerant blend with the learned journal mean if both exist.
        learned = tune["clim_mean"]
        if learned is not None:
            # 50/50 blend: the journal mean reflects THIS site's settled days,
            # the METAR climatology has more samples.  Weight the one with more
            # data-equivalent confidence — the learned mean is intrinsically
            # smaller-sample, so cap its blend weight at 0.35.
            w_learned = min(0.35, tune["n_settled"] / 20.0)
            prior = (1.0 - w_learned) * clim_mean + w_learned * learned
        else:
            prior = clim_mean
        prior += tune["trend"]
    elif tune["clim_mean"] is not None:
        prior = tune["clim_mean"] + tune["trend"]  # drift-aware journal prior
    elif nea_high is not None and 28.0 <= nea_high <= 38.0:
        prior = nea_high
    else:
        prior = CLIM_MEAN_DEFAULT
    predicted_mean = w * predicted_mean + (1.0 - w) * prior
    predicted_mean += tune["bias"]

    # Per-hour bias correction (learned, replaces the static MORNING_BIAS_HOURS
    # once enough days have settled).  The learned curve captures the same
    # morning under-prediction AND the afternoon over-prediction that the static
    # table could not express.  Falls back to the static morning table until the
    # learned curve is ready.
    learned_hours = tune["hourly_bias"].get("hours", {})
    hr_key = str(int(hr))
    if learned_hours:
        predicted_mean += learned_hours.get(hr_key, tune["bias"])
    else:
        predicted_mean += MORNING_BIAS_HOURS.get(int(hr), 0.0)
        # Apply the learned trend on top of the static morning correction —
        # even before the hourly curve engages, a drifting regime is real.
        predicted_mean += 0.5 * tune["trend"]

    # Upper cap with a live NEA forecast: never predict meaningfully above both
    # the running max and the official forecast (+1.0°C tolerance). A cool
    # forecast can't be overridden by model optimism; a hot one already pulls mu
    # up through the blend. Without a forecast the cap is skipped (preserves the
    # unconstrained pre-change behavior).
    if nea_high is not None:
        cap = max(current_max, nea_high + 1.0)
        predicted_mean = min(predicted_mean, cap)

    # Mild sigma floor when live signal is weak (w < 0.5). Avoids full blend which
    # would shrink midday sigma and degrade journal hit-rates; this only raises
    # uncertainty where we genuinely don't have a formed signal. Uses the learned
    # sigma floor once tuning has engaged (it tightens as real data accrues).
    if w < 0.5:
        sigma_floor = tune["sigma_floor"]
        # Data-driven climatological spread (from the METAR cache) is a better
        # floor than the learned journal spread when the journal is thin.
        if clim_std is not None:
            sigma_floor = max(sigma_floor, 0.7 * clim_std)
        predicted_std = max(predicted_std, 0.85 * sigma_floor)

    # Post-hoc sigma calibration: multiply by the learned |error|/sigma ratio
    # once it has settled on a stable value.  When the model has been over-
    # confident (observed errors running wider than sigma), scale sigma UP so
    # bracket probabilities stop claiming near-certainty on diffuse days.  The
    # scale is clamped so a bad week can't blow sigma out to nonsense.
    predicted_std *= tune["uncertainty_scale"]

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


def evaluate_polymarket_brackets(event_date_str: str, mean_temp: float, std_temp: float, markets: list[dict], todays_max_so_far: float = None, hour_of_day: float = None, live_prices: list[dict] = None) -> dict:
    """
    Price Polymarket Binary Options using the predicted probability distribution,
    size each trade with the Kelly criterion, then apply a portfolio-level cap
    across brackets (they're mutually exclusive outcomes of the same event).

    Returns the full structured result so HTTP callers (server.py) can serialize
    it; the terminal output below is unchanged for the CLI loop.
    """
    print(f"\n--- LIVE PREDICTION for {event_date_str} ({datetime.now(SGT).strftime('%H:%M:%S SGT')}) ---")
    print(f"Predicted Max Temp (WSSS): {mean_temp:.2f}°C (±{std_temp:.2f}°C)\n")

    # Live-price lookup keyed by bracket title. The WS snapshot carries the same
    # best_ask/best_bid the UI renders via /api/prices; when present, the trade
    # path prices edge off these quotes instead of Gamma metadata.
    _live_by_bracket = {b.get("bracket"): b for b in (live_prices or [])}

    # Confidence-scaled minimum edge for this cycle: on diffuse days the engine
    # demands more edge before committing, so the 1% floor is only the absolute
    # floor — in practice it's higher when the model is less sure.
    min_edge = compute_effective_min_edge(std_temp)

    trades = []
    for m in markets:
        title = m["group_item_title"] or m["question"]
        low, high = parse_temperature_bounds(title)
        prob = calculate_bracket_probability(low, high, mean_temp, std_temp)
        # ── FLOOR CHECK ──────────────────────────────────────────────────
        # Once today's running max has reached or exceeded a bracket's upper
        # bound, that bracket is impossible and no longshot BUY_YES makes
        # sense: the floor has already closed above it.  Skip and let the
        # entry gate log a CLEAN_SKIP so the history stays noise-free.
        # Conversely, if the bracket's LOWER bound is above today's max by
        # more than 2σ + mean headroom, also skip — we're chasing a tail
        # that the model's own uncertainty can't justify.
        # ──────────────────────────────────────────────────────────────────
        if todays_max_so_far is not None and high is not None and todays_max_so_far > high:
            # Running max has already exceeded the bracket ceiling → impossible.
            trades.append({
                "bracket": title, "prob": 0.0, "price": m.get("best_ask", 0) or 0,
                "yes_price": m.get("best_ask"), "no_price": m.get("no_price"),
                "yes_sell": m.get("best_bid"), "no_sell": m.get("no_bid"),
                "action": "SKIP",
                "reason": f"Floor exceeded: running max {todays_max_so_far:.2f}°C > bracket ceiling {high:.2f}°C",
                "edge": 0.0, "stake_usd": 0.0,
            })
            continue
        # Use Gamma's bestAsk (accurate for negRisk brackets); fall back to the
        # CLOB book if the metadata is missing it.
        gamma_ask = m.get("best_ask")
        if gamma_ask is None:
            gamma_ask = get_live_clob_price(m["yes_token_id"])
        gamma_bid = m.get("best_bid")
        no_price_gamma = m.get("no_price")
        no_bid_gamma = m.get("no_bid")

        # --- LIVE PRICE OVERRIDE from WebSocket feed ---
        # The WS snapshot carries the same best_ask/best_bid the UI renders via
        # /api/prices. When present and sane, override Gamma metadata so the
        # model's edge is computed against the same quotes the user sees.
        live_entry = _live_by_bracket.get(title) if _live_by_bracket else None
        priced_from_live = False
        yes_bid = gamma_bid
        if live_entry is not None:
            ws_ask = live_entry.get("yes")     # WS best_ask
            ws_bid = live_entry.get("yes_sell") # WS best_bid
            if ws_ask is not None and MIN_LIVE_ASK <= ws_ask <= MAX_ASK_TO_TRADE:
                price = ws_ask
                priced_from_live = True
                if ws_bid is not None:
                    yes_bid = ws_bid
            else:
                price = gamma_ask
            # Recompute NO side via simple complement (mirrors polymarket.py:95-106)
            if priced_from_live:
                no_price = live_entry.get("no")   # WS NO buy = 1 - yes_sell(bid)
                no_bid = live_entry.get("no_sell")  # WS NO sell = 1 - yes_ask
            else:
                no_price = no_price_gamma
                no_bid = no_bid_gamma
        else:
            price = gamma_ask
            no_price = no_price_gamma
            no_bid = no_bid_gamma

        # --- LIQUIDITY GUARD ---
        # Refuse to trade when the quote is too thin (< $0.01) or near-certain
        # (>= $0.97) — these are noise, not edge.
        if price is None or price < MIN_LIVE_ASK or price >= MAX_ASK_TO_TRADE:
            trades.append({
                "bracket": title, "prob": prob, "price": price or 0,
                "yes_price": price, "no_price": no_price,
                "yes_sell": gamma_bid, "no_sell": no_bid,
                "priced_from_live": priced_from_live,
                "action": "SKIP",
                "reason": f"Thin market: ask {'missing' if price is None else f'${price:.3f}' } outside safe range",
                "edge": 0.0, "stake_usd": 0.0,
            })
            continue

        trade = compute_kelly_trade(
            prob, price,
            no_price=no_price,
            yes_bid=yes_bid,
            no_bid=no_bid,
            min_edge=min_edge,
        )
        trade["bracket"] = title
        trade["prob"] = prob
        trade["price"] = price
        trade["yes_price"] = trade.get("yes_price", price)
        trade["no_price"] = trade.get("no_price", no_price)
        trade["yes_sell"] = yes_bid
        trade["no_sell"] = no_bid
        trade["priced_from_live"] = priced_from_live
        trade["min_edge_applied"] = round(min_edge, 3)
        trades.append(trade)

    # --- SIGNAL GATE ---
    # Overnight / outside the tradeable window there is no formed signal: mu is
    # heavily pulled toward the climatological prior and the book is thin, so any
    # "edge" computed now is noise. Rewrite staged BUY actions to NO_SIGNAL so the
    # entry gate can't open a position. Carve-out: near-final lockout (df >= 0.97,
    # heating essentially done) keeps legitimate late-day trades alive.
    if hour_of_day is not None:
        df = _diurnal_heating_fraction(hour_of_day)
        tradeable = (TRADEABLE_HOURS_START <= hour_of_day <= TRADEABLE_HOURS_END) or (df >= 0.97)
        if not tradeable:
            for t in trades:
                if t.get("action") in ("BUY_YES", "BUY_NO"):
                    t["action"] = "NO_SIGNAL"
                    t["stake_usd"] = 0.0
                    t["reason"] = (
                        f"Overnight — no formed signal yet ({int(hour_of_day):02d}:00 SGT); "
                        f"edge {t.get('edge', 0.0) * 100:.1f}% is model noise vs a thin book"
                    )

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
            evaluate_polymarket_brackets(
                event_date_str, mu, sigma, markets,
                todays_max_so_far=features.get("wsss_todays_max_so_far"),
                hour_of_day=features.get("hour_of_day"),
            )
        else:
            print(f"[!] No live event found within the next {MAX_DAYS_AHEAD_TO_CHECK} days. Retrying next cycle...")

        print(f"\n[~] Sleeping for {POLL_INTERVAL_SECONDS} seconds before next API pull...")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        print("\n[!] Exiting continuous evaluation loop.")