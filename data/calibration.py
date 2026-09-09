"""Sigma-calibration backtest — is the model's claimed uncertainty honest?

The live model emits (mu, sigma) for the WSSS daily max.  Its bracket "edge"
and its Kelly sizing both hinge on sigma: a 0.15°C claim means P(bracket)≈1.0
and near-certainty, while empirically the day-to-day residual on the daily max
runs closer to ±1-2°C.  When sigma is understated, the model over-trades its own
noise and the guard rails immediately de-rate the brackets it just entered.

This module replays the REAL prediction function (main.predict_daily_max_temp)
over historical WSSS METAR days.  For each (day, hour) it reconstructs the
feature vector the pipeline would have produced from that day's METAR reports
alone (current temp, running max, dew point, pressure trend, cloud, ramp; UV /
NEA / spatial signals are stubbed to neutral) and measures:

    error   = actual_max - predicted_mu
    coverage_1σ / coverage_2σ
      fraction of |error| <= 1σ / 2σ.  A well-calibrated normal model gives
      ~68% / ~95%.  Miscalibration (too-small sigma) shows up as coverage far
      below those targets.

    brier   = mean over settled days+hours of
              SUM_brackets (P_model(bracket) - 1[actual in bracket])^2
      Lower Brier = sharper, better-calibrated bracket probabilities.

Ground truth is the WSSS METAR daily max ONLY (same settlement source as the
prediction journal).  Cached in data/wsss_metar_cache.json so repeated runs
don't re-hit the aviationweather API.

This is the evaluation surface for sigma changes: run it on the old formula
("before"), change _hour_based_sigma, run it again ("after"), and only keep
the change if coverage moves toward the 68/95 targets without degrading Brier.
"""

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from main import predict_daily_max_temp

SGT = ZoneInfo("Asia/Singapore")
CACHE_FILE = Path("data/wsss_metar_cache.json")
METAR_URL = "https://aviationweather.gov/api/data/metar"
# Hours to replay (the trading-relevant day).  Morning = the window the entry
# gate actually uses (ENTRY_WINDOW_HOURS 10-15).
REPLAY_HOURS = [8, 9, 10, 11, 12, 13, 14, 15, 16, 18, 20]

# Feature stubs for signals we can't reconstruct from METAR alone.  Neutral
# values that neither inflate nor deflate the forecast much.
_STUB = {
    # Solar term uses max(0.2, uv/11). 0 badly understates morning headroom and
    # inflates the model's (apparent) morning under-predict; 5 is a representative
    # mid-range Singapore UVI that keeps the solar scaling realistic without
    # manufacturing sunny-day optimism.
    "uv_index": 5.0,
    "lightning_strike_count": 0,
    "rain_station_ratio": 0.0,
    "rain_dist_to_changi_km": None,
    "changi_forecast_storm": 0.0,
    "wsss_storm_txt": 0.0,
    "spatial_temp_spread": None,
    "nea_forecast_high": None,
    "nea_forecast_low": None,
}


def _load_metar(fetch: bool = True) -> list:
    """METAR history oldest-first. Cached on disk; fetched when missing."""
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE) as f:
                cached = json.load(f)
            if cached:
                return cached
        except (json.JSONDecodeError, IOError):
            pass
    if not fetch:
        return []
    resp = requests.get(METAR_URL, params={
        "ids": "WSSS", "format": "json", "taf": "false", "hours": "720",
    }, timeout=30)
    resp.raise_for_status()
    metars = resp.json()
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(metars, f, indent=2)
    return metars


def _features_for(day_obs, hour: int) -> dict:
    """Reconstruct the feature vector the pipeline would see at `hour` of this
    day, from that day's METAR reports so far. Returns None if no report <= hour."""
    target = datetime.combine(day_obs[0]["date"], datetime.min.time()).replace(hour=hour, tzinfo=SGT)
    before = [o for o in day_obs if o["dt"] <= target]
    if not before:
        return None
    # Skip partial days that don't have at least some morning observations
    if hour >= 10 and not any(o["dt"].hour < 10 for o in day_obs):
        return None
    latest = before[-1]
    cur = latest["temp"]
    run_max = max(o["temp"] for o in before)
    # 3h ramp / pressure trend vs the report ~3h earlier.
    ref = target - timedelta(hours=3)
    older = next((o for o in before if o["dt"] <= ref), None)
    ramp = cur - (older["temp"] if older else cur)
    press_trend = latest["altim"] - (older["altim"] if older else latest["altim"])
    dpd = cur - latest["dewp"]
    features = dict(_STUB)
    features.update({
        "wsss_current_temp": cur,
        "wsss_todays_max_so_far": run_max,
        "wsss_dewp": latest["dewp"],
        "wsss_temp_ramp_3h": round(ramp, 1),
        "wsss_press_trend_3h": round(press_trend, 1),
        "wsss_dpd": round(max(0.0, dpd), 1),
        "wsss_low_cloud_ft": latest.get("low_cloud_ft", 0.0),
        "wsss_total_cloud_oktas": latest.get("cloud_oktas", 4.0),
        "minutes_since_last_metar": 0.0,
        "hour_of_day": float(hour),
    })
    return features


def _parse_metar(metars: list) -> list:
    """Turn raw METAR JSON into day-indexed observations, oldest-first."""
    obs = []
    for m in metars:
        temp = m.get("temp")
        if temp is None:
            continue
        eps = m.get("obsTime", 0)
        if not eps:
            continue
        dt = datetime.fromtimestamp(eps, tz=SGT)
        altim = m.get("altim")
        try:
            altim = float(altim) if altim else 1009.0
        except (TypeError, ValueError):
            altim = 1009.0
        clouds = m.get("clouds") or []
        oktas = 0.0
        lowest_base = 0.0
        for layer in clouds:
            cover = str(layer.get("cover", "FEW")).upper()
            oktas += {"CLR": 0, "SKC": 0, "FEW": 2, "SCT": 4, "BKN": 7, "OVC": 8}.get(cover, 2)
            base = float(layer["base"]) if layer.get("base") else 0.0
            if base > 0 and (lowest_base == 0 or base < lowest_base):
                lowest_base = base
        obs.append({
            "dt": dt,
            "date": dt.date(),
            "temp": float(temp),
            "dewp": float(m.get("dewp") or 24.0),
            "altim": altim,
            "cloud_oktas": min(8.0, oktas),
            "low_cloud_ft": lowest_base,
        })
    obs.sort(key=lambda o: o["dt"])
    return obs


def replay() -> dict:
    """Run the live prediction function over every (day, hour). Returns rows."""
    metars = _load_metar()
    obs = _parse_metar(metars)
    by_day = defaultdict(list)
    for o in obs:
        by_day[o["date"]].append(o)
    for v in by_day.values():
        v.sort(key=lambda o: o["dt"])

    rows = []
    for date in sorted(by_day):
        day_obs = by_day[date]
        actual = max(o["temp"] for o in day_obs)
        for hr in REPLAY_HOURS:
            feats = _features_for(day_obs, hr)
            if feats is None:
                continue
            mu, sigma = predict_daily_max_temp(feats)
            rows.append({
                "date": date.isoformat(),
                "hour": hr,
                "actual": actual,
                "mu": mu,
                "sigma": sigma,
                "error": actual - mu,
                "features": feats,
            })
    return rows


def _bracket_probs(mu, sigma, brackets) -> dict:
    """Model probability mass per °C bracket over [28..39]."""
    from execution.kelly_sizer import calculate_bracket_probability
    probs = {}
    for lo, hi in brackets:
        probs[f"{int(lo)}"] = calculate_bracket_probability(lo, hi, mu, sigma)
    return probs


_BRACKETS = [(28, 28.5), (28.5, 29.5), (29.5, 30.5), (30.5, 31.5),
             (31.5, 32.5), (32.5, 33.5), (33.5, 34.5), (34.5, 35.5),
             (35.5, 36.5), (36.5, 37.5), (37.5, 38.5)]


def run_calibration(rows: list | None = None) -> dict:
    """Coverage + Brier over the replay rows."""
    rows = rows if rows is not None else replay()
    if not rows:
        return {"error": "no data", "rows": []}

    n = len(rows)
    cov1 = sum(1 for r in rows if abs(r["error"]) <= max(0.1, r["sigma"])) / n
    cov2 = sum(1 for r in rows if abs(r["error"]) <= 2 * max(0.1, r["sigma"])) / n

    # Brier over settled days+hours
    brier = 0.0
    for r in rows:
        probs = _bracket_probs(r["mu"], r["sigma"], _BRACKETS)
        brier += sum(
            (p - (1.0 if (lo <= r["actual"] < hi) else 0.0)) ** 2
            for (lo, hi), p in zip(_BRACKETS, probs.values())
        )
    brier /= n

    mae = sum(abs(r["error"]) for r in rows) / n
    rmse = math.sqrt(sum(r["error"] ** 2 for r in rows) / n)

    # Per-hour coverage (so a fix can be checked hour by hour)
    by_hour = defaultdict(list)
    for r in rows:
        by_hour[r["hour"]].append(r)
    per_hour = {}
    for hr in sorted(by_hour):
        h = by_hour[hr]
        hc1 = sum(1 for r in h if abs(r["error"]) <= max(0.1, r["sigma"])) / len(h)
        per_hour[hr] = {"n": len(h), "cov1": round(hc1, 3),
                        "mae": round(sum(abs(r["error"]) for r in h) / len(h), 3)}

    return {
        "days": len({r["date"] for r in rows}),
        "samples": n,
        "mae": round(mae, 3),
        "rmse": round(rmse, 3),
        "coverage_1sigma": round(cov1, 3),
        "coverage_2sigma": round(cov2, 3),
        "target_1sigma": 0.68,
        "target_2sigma": 0.95,
        "avg_sigma": round(sum(r["sigma"] for r in rows) / n, 3),
        "per_hour": per_hour,
        "mean_error": round(sum(r["error"] for r in rows) / n, 3),
    }


if __name__ == "__main__":
    import sys
    if "--no-cache" in sys.argv:
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()
    st = run_calibration()
    print(json.dumps(st, indent=2))