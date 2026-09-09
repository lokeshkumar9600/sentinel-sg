"""
Temporal decay and time-of-day scaling for the convection-storm suppression score.

This module is a pure-function additive layer: it takes the existing storm score
(from main.py's _convection_storm_score) and applies temporal context so that
stale signals don't keep capping the peak all afternoon, and the suppression
scales with Singapore's diurnal convective cycle.

All functions are deterministic, side-effect-free, and bounded in [0, 1].
"""

from math import cos, pi

# ---------------------------------------------------------------------------
# Config constants (local defaults, NOT in data/config.py)
# ---------------------------------------------------------------------------
FORECAST_MAX_AGE_MIN = 60       # minutes after which forecast is considered stale
HOUR_PEAK = 14                  # hour (SGT) of peak convective activity
HOUR_AMPLITUDE = 0.4            # half-amplitude of time-of-day modulation (0.4 -> range [0.6, 1.0])
AGE_MIN = 30                    # minutes: METAR age above which decay starts
AGE_MAX_AS_MULT = 0.4           # asymptotic floor for age penalty (never fully ignore a storm)


def _age_penalty(minutes_since_metar: float | None, forecast_age_min: float | None) -> float:
    """
    Compute age-based decay multiplier in [AGE_MAX_AS_MULT, 1.0].

    - If METAR is > AGE_MIN minutes old, decay starts.
    - If forecast is > FORECAST_MAX_AGE_MIN minutes old, decay also applies.
    - Both decay monotonically with age, asymptoting at AGE_MAX_AS_MULT.
    """
    # METAR age component
    metar_penalty = 1.0
    if minutes_since_metar is not None and minutes_since_metar > AGE_MIN:
        # Linear decay from 1.0 at AGE_MIN to AGE_MAX_AS_MULT at large ages
        # Using a smooth curve: 1 - (1 - floor) * (age - AGE_MIN) / (age - AGE_MIN + scale)
        scale = 60.0  # decay scale in minutes
        excess = minutes_since_metar - AGE_MIN
        metar_penalty = 1.0 - (1.0 - AGE_MAX_AS_MULT) * (excess / (excess + scale))
        metar_penalty = max(AGE_MAX_AS_MULT, min(1.0, metar_penalty))

    # Forecast age component (if available)
    forecast_penalty = 1.0
    if forecast_age_min is not None and forecast_age_min > FORECAST_MAX_AGE_MIN:
        excess = forecast_age_min - FORECAST_MAX_AGE_MIN
        forecast_penalty = 1.0 - (1.0 - AGE_MAX_AS_MULT) * (excess / (excess + scale))
        forecast_penalty = max(AGE_MAX_AS_MULT, min(1.0, forecast_penalty))

    # Combine: the more restrictive (lower) penalty wins
    return min(metar_penalty, forecast_penalty)


def _time_of_day_penalty(hour_of_day: float | None) -> float:
    """
    Compute time-of-day multiplier in [0.6, 1.0] using a cosine/Hann window
    centered on HOUR_PEAK (14:00 SGT).

    Peak suppression at 14:00 (factor=1.0), minimum at pre-dawn (factor=0.6).
    Smooth transitions, no sharp hour boundaries.
    """
    if hour_of_day is None:
        return 1.0  # neutral if unknown

    # Cosine window: cos^2 goes from 0 to 1; we map [0, 1] -> [0.6, 1.0]
    # At HOUR_PEAK, phase = 0 -> cos(0) = 1 -> factor = 1.0
    # At HOUR_PEAK +/- 12h, phase = pi -> cos(pi) = -1 -> factor = 0.6
    phase = 2.0 * pi * (hour_of_day - HOUR_PEAK) / 24.0
    cos_sq = cos(phase) ** 2
    # Map cos^2 in [0, 1] to [1 - HOUR_AMPLITUDE, 1] = [0.6, 1.0]
    return (1.0 - HOUR_AMPLITUDE) + HOUR_AMPLITUDE * cos_sq


def compute_storm_timing_factor(features: dict) -> dict:
    """
    Compute temporal scaling factors for the storm suppression score.

    Args:
        features: Feature dict from extract_singapore_feature_vector. Expected keys:
            - minutes_since_last_metar (float): age of latest METAR in minutes
            - hour_of_day (int/float): current hour in SGT (0-23)
            - changi_forecast_storm (float): 1.0 if NEA 2hr forecast shows thunder
            - nea_forecast_issued_at (float, optional): epoch seconds when forecast was issued

    Returns:
        dict with keys:
            - storm_age_penalty: float in [0.4, 1.0]
            - time_of_day_penalty: float in [0.6, 1.0]
            - combined: float in [0, 1] (product of penalties, for direct multiplication with storm score)
            - signals: dict with debug breakdown
    """
    minutes_since_metar = features.get("minutes_since_last_metar")
    hour_of_day = features.get("hour_of_day")

    # Forecast age: if we have the forecast issue timestamp, compute age
    forecast_age_min = None
    fc_issued = features.get("nea_forecast_issued_at")
    if fc_issued is not None:
        import time
        forecast_age_min = max(0.0, (time.time() - fc_issued) / 60.0)

    age_penalty = _age_penalty(minutes_since_metar, forecast_age_min)
    time_penalty = _time_of_day_penalty(hour_of_day)
    combined = age_penalty * time_penalty

    return {
        "storm_age_penalty": round(age_penalty, 4),
        "time_of_day_penalty": round(time_penalty, 4),
        "combined": round(combined, 4),
        "signals": {
            "minutes_since_metar": minutes_since_metar,
            "forecast_age_min": forecast_age_min,
            "hour_of_day": hour_of_day,
            "metar_age_exceeds_threshold": (minutes_since_metar is not None and minutes_since_metar > AGE_MIN),
            "forecast_age_exceeds_threshold": (forecast_age_min is not None and forecast_age_min > FORECAST_MAX_AGE_MIN),
        },
    }


def refine_storm_score(storm_score: float, timing: dict) -> float:
    """
    Apply temporal penalties to the raw storm score.

    Args:
        storm_score: Raw storm score from _convection_storm_score in [0, 1]
        timing: Output dict from compute_storm_timing_factor

    Returns:
        Refined storm score in [0, 1]
    """
    combined = timing.get("combined", 1.0)
    return max(0.0, min(1.0, storm_score * combined))


def apply_storm_history(score: float, hour: float) -> float:
    """
    Smooth time-of-day modulation using a Hann-like window around the convective peak.

    This is an alternative to refine_storm_score that only applies the time-of-day
    factor (no METAR/forecast age), using a cosine-squared window for smoothness.
    Useful for historical/replay analysis where only the hour is known.

    Args:
        score: Storm score in [0, 1]
        hour: Hour of day in SGT (0-23, can be fractional)

    Returns:
        Time-modulated score in [0, 1]
    """
    time_penalty = _time_of_day_penalty(hour)
    return max(0.0, min(1.0, score * time_penalty))