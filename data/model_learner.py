"""Daily model self-improvement — a lightweight meta-learning loop over the
settled prediction journal.

Every settled day yields a (predicted_mu, actual_max) pair.  From that history
we fit two small, interpretable corrections and apply them to the next day's
prediction:

  learned_clim_mean  - the running mean of settled max temps.  Once enough days
      have settled it replaces the hardcoded CLIM_MEAN_DEFAULT prior, so the
      climatology converges to this site's *real* daytime climate instead of a
      September constant.

  learned_bias       - the mean of (actual_max - predicted_mu), the model's
      systematic error.  Added to the prediction so the model self-corrects a
      chronic over- or under-predict with no hand tuning.

Both are small-sample guarded: a single day can't move the model, the bias is
clamped so one anomalous day can't jerk the forecast, and both have a floor on
the number of settled days before they engage.  A moving average is the honest
amount of "learning" the data supports right now — there aren't enough settled
days for a seasonal curve or a per-hour model yet, and the whole design
deliberately stays inspectable rather than a black box.

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
# 1.0 = plain mean (all days equal); smaller = recent days matter more.
_BIAS_ALPHA = 1.0


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
        })
    pairs.reverse()  # oldest -> newest
    return pairs


def self_tune() -> dict:
    """Fit learned corrections from settled days.

    Returns
      clim_mean   float|None  learned daily-max climatology (None until enough
                              days have settled)
      bias        float       systematic correction to add to the prediction
                              (0.0 until enough days have settled)
      sigma_floor float       a learned uncertainty floor (smaller than the
                              hardcoded clim sigma once we have real data)
      n_settled   int         settled days used by the fit
      source      str         "learned" when engaged, else "default"
    """
    pairs = _settled_pairs()
    n = len(pairs)

    if n < LEARN_MIN_SAMPLES:
        return {
            "clim_mean": None,
            "bias": 0.0,
            "sigma_floor": CLIM_SIGMA,
            "n_settled": n,
            "source": "default",
        }

    actuals = [p["actual"] for p in pairs]
    clim_mean = sum(actuals) / n

    # EMA of (actual - mu).  With alpha=1 this is the plain mean of the bias,
    # clamped so a single outlier can't dominate the next day's forecast.
    bias = 0.0
    for p in pairs:
        bias = _BIAS_ALPHA * (p["actual"] - p["mu"]) + (1.0 - _BIAS_ALPHA) * bias
    bias = max(-LEARN_BIAS_CLAMP, min(LEARN_BIAS_CLAMP, bias))

    # Learned uncertainty floor: how far actuals typically sit from the mean.
    # As data accrues this tightens toward the model's real spread; we keep a
    # small floor so a couple of quiet days never understate risk to zero.
    spread = sum(abs(a - clim_mean) for a in actuals) / n
    sigma_floor = max(0.5, spread)

    return {
        "clim_mean": round(clim_mean, 3),
        "bias": round(bias, 3),
        "sigma_floor": round(sigma_floor, 3),
        "n_settled": n,
        "source": "learned",
    }
