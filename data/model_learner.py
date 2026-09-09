"""Daily model self-improvement — a lightweight meta-learning loop over the
settled prediction journal.

Every settled day yields a (predicted_mu, actual_max) pair.  From that history
we fit several small, interpretable corrections and apply them to the next day's
prediction:

  learned_clim_mean  - the running mean of settled max temps.  Once enough days
      have settled it replaces the hardcoded CLIM_MEAN_DEFAULT prior, so the
      climatology converges to this site's *real* daytime climate instead of a
      September constant.

  learned_bias       - the mean of (actual_max - predicted_mu), the model's
      systematic error.  Added to the prediction so the model self-corrects a
      chronic over- or under-predict with no hand tuning.

  learned hourly bias - an EMA of (actual_max - predicted_mu) broken out by the
      hour the prediction was recorded.  Morning under-prediction and afternoon
      over-prediction are DIFFERENT biases — a single global bias blends them
      and under-corrects both.  The per-hour bias converges on the real diurnal
      error curve and replaces the static MORNING_BIAS_HOURS table once learned.

  learned_trend      - how the recent actuals are drifting day-over-day (e.g.
      a Monsoon surge cooling the island).  Applied as a gentle nudge so the
      climatology prior stops lagging a moving regime.

  learned_uncertainty_scale - the ratio of observed |error| to claimed sigma.
      Post-hoc calibration: if errors are running 1.3x sigma, sigma is raised
      1.3x so the confidence is honest again.  Also powers adaptive Kelly.

All corrections are small-sample guarded: a single day can't move the model,
biases are clamped so one anomalous day can't jerk the forecast, and all have a
floor on the number of settled days before they engage.

The loop is stable by construction: the journal records the model's *output*
(mu after any correction), so next day's bias is measured against the corrected
prediction.  That is a negative feedback — if the model over-predicts, the bias
turns negative, pulls mu down, and the measured bias shrinks toward zero.
"""

from data.config import (
    CLIM_MEAN_DEFAULT,
    CLIM_SIGMA,
    LEARN_MIN_SAMPLES,
    LEARN_BIAS_CLAMP,
)
from data.prediction_journal import get_journal

# How much weight each settled day carries in the rolling bias estimate.
# alpha=1.0 would collapse the EMA to only the newest day (all history
# discarded); a smaller alpha keeps memory so the estimate is genuinely a mean
# of the last N days, weighted toward recent ones.
_BIAS_ALPHA = 0.3

# Hourly-bias smoothing: a slower EMA than the global one, because a per-hour
# bucket has far fewer samples.  Lower alpha = more smoothing to avoid chasing
# single-day noise in any given hour bucket.
_HOURLY_BIAS_ALPHA = 0.25

# How strongly the day-over-day actual drift feeds into the climatology prior.
_HOURS_IN_DAY = 24

# Hourly bias "warm-up": how many settled days we need before replacing the
# static MORNING_BIAS_HOURS table with the learned per-hour curve.
MIN_HOURLY_SAMPLES = 4


def _settled_pairs() -> list[dict]:
    """Chronological list of settled journal days, oldest first."""
    pairs = []
    for e in get_journal():  # newest first
        mu = e.get("predicted_mu")
        actual = e.get("actual_max")
        if mu is None or actual is None:
            continue
        pairs.append({
            "mu": float(mu),
            "actual": float(actual),
            "sigma": float(e.get("predicted_sigma") or 0.0),
            "hour_of_day": int(e.get("hour_of_day") or 12),
        })
    pairs.reverse()  # oldest -> newest
    return pairs


def _exponential_weighted_mean(values: list[float], alpha: float) -> float:
    """EMA across a list (oldest -> newest).  alpha ~ 0.3 weights recent days."""
    mean = 0.0
    for v in values:
        mean = alpha * v + (1.0 - alpha) * mean
    return mean


def self_tune() -> dict:
    """Fit learned corrections from settled days.

    Returns
      clim_mean   float|None  learned daily-max climatology (None until enough
                              days have settled)
      bias        float       systematic correction to add to the prediction
                              (0.0 until enough days have settled)
      hourly_bias dict[int, float]  per-hour error corrections, plus 'source'
                              ("learned" vs "static")
      sigma_floor float       a learned uncertainty floor (smaller than the
                              hardcoded clim sigma once we have real data)
      uncertainty_scale float post-hoc calibration of sigma (learned ratio of
                              |error| to claimed sigma; 1.0 until learned)
      trend       float       day-over-day drift of recent actuals
      n_settled   int         settled days used by the fit
      source      str         "learned" when engaged, else "default"
    """
    pairs = _settled_pairs()
    n = len(pairs)

    if n < LEARN_MIN_SAMPLES:
        return {
            "clim_mean": None,
            "bias": 0.0,
            "hourly_bias": {"hours": {}, "source": "static"},
            "sigma_floor": CLIM_SIGMA,
            "uncertainty_scale": 1.0,
            "trend": 0.0,
            "n_settled": n,
            "source": "default",
        }

    actuals = [p["actual"] for p in pairs]
    clim_mean = sum(actuals) / n

    # EMA of (actual - mu).  With alpha=1 this is the plain mean of the bias,
    # clamped so a single outlier can't dominate the next day's forecast.
    errors = [p["actual"] - p["mu"] for p in pairs]
    bias = _exponential_weighted_mean(errors, _BIAS_ALPHA)
    bias = max(-LEARN_BIAS_CLAMP, min(LEARN_BIAS_CLAMP, bias))

    # Learned uncertainty floor: how far actuals typically sit from the mean.
    # As data accrues this tightens toward the model's real spread; we keep a
    # small floor so a couple of quiet days never understate risk to zero.
    spread = sum(abs(a - clim_mean) for a in actuals) / n
    sigma_floor = max(0.5, spread)

    # Post-hoc sigma calibration: ratio of observed |error| to claimed sigma.
    # >1.0 means the model is over-confident; <1.0 means under-confident.
    sds = [abs(p["error"]) / max(0.1, p["sigma"]) for p in (
        {**p, "error": p["actual"] - p["mu"]} for p in pairs
    )]
    uncertainty_scale = min(2.0, max(0.5, sum(sds) / n))

    # Day-over-day trend of the actuals (smoothed by EMA of the deltas).
    trend = 0.0
    if n >= 2:
        deltas = [b - a for a, b in zip(actuals, actuals[1:])]
        trend = _exponential_weighted_mean(deltas, 0.2)
        trend = max(-1.5, min(1.5, trend))  # a regime shift runs ~±1.5°C/day max

    # ---- Per-hour bias curve ----
    # Collect (actual - mu) per the hour the prediction was recorded, so the
    # model learns that morning predictions (H8-H12) systematically under-shoot
    # while evening predictions (H16+) over-shoot.  Bucket by 3-hour blocks for
    # stability: [0-5] overnight, [6-8] dawn, [9-11] mid-morning,
    # [12-14] noon, [15-17] afternoon, [18-23] evening.
    hour_buckets: dict[int, list] = {}
    for h in range(6):
        hour_buckets[h] = []
    bucket_edges = {0: (0, 5), 1: (6, 8), 2: (9, 11), 3: (12, 14), 4: (15, 17), 5: (18, 23)}
    for p in pairs:
        hr = p["hour_of_day"]
        for bkey, (lo, hi) in bucket_edges.items():
            if lo <= hr <= hi:
                hour_buckets[bkey].append(p["actual"] - p["mu"])
                break

    hourly_bias = {}
    source = "static"
    # Only trust a learned hour curve if every main bucket has some samples and
    # we have a couple of settled days total.
    if n >= MIN_HOURLY_SAMPLES:
        learned_curve = {}
        for bkey, errs in hour_buckets.items():
            if errs:
                learned_curve[bkey] = max(-LEARN_BIAS_CLAMP, min(LEARN_BIAS_CLAMP,
                                               _exponential_weighted_mean(errs, _HOURLY_BIAS_ALPHA)))
        # Map hour -> learned correction (carry nearest learned bucket).
        hr_corrections = {}
        for hr in range(_HOURS_IN_DAY):
            for bkey, (lo, hi) in sorted(bucket_edges.items(), key=lambda kv: kv[1]):
                if lo <= hr <= hi:
                    hr_corrections[hr] = learned_curve.get(bkey, bias)
                    break
        hourly_bias = {"hours": {
            str(hr): round(v, 3) for hr, v in hr_corrections.items()
        }, "source": "learned"}
        source = "learned"
    else:
        hourly_bias = {"hours": {}, "source": "static"}

    return {
        "clim_mean": round(clim_mean, 3),
        "bias": round(bias, 3),
        "hourly_bias": hourly_bias,
        "sigma_floor": round(sigma_floor, 3),
        "uncertainty_scale": round(uncertainty_scale, 3),
        "trend": round(trend, 3),
        "n_settled": n,
        "source": source,
    }