import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import requests

from data.config import TWO_HR_CHANGI_AREAS

CHANGI_LAT = 1.3644
CHANGI_LON = 103.9915
SGT = ZoneInfo("Asia/Singapore")

# Cloud-cover -> eighths, for summing total cloud amount from METAR layers.
_COVER_OKTA = {"CLR": 0, "SKC": 0, "FEW": 2, "SCT": 4, "BKN": 7, "OVC": 8, "OVX": 8, "VV": 8}
_THUNDER_TOKENS = ("thunder", "tsra", "tssn", "shower", "rain", "drizzle", "+tsra")
_RAIN_HOTSPOT_MM = 1.0  # a station counts as a rain "hotspot" above this (mm)


def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0  # Earth radius in km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _sgt_date_of(obs_time_epoch: float):
    return datetime.fromtimestamp(obs_time_epoch, tz=timezone.utc).astimezone(SGT).date()


def _f(v, default: float):
    """Coerce to float, or return default on None/garbage."""
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _rh_from_dewpoint(temp_c: float, dewp_c: float) -> float:
    """Magnus formula for relative humidity from temp/dewpoint (%)."""
    a, b = 17.625, 243.04
    try:
        gamma_t = (a * temp_c) / (b + temp_c)
        gamma_d = (a * dewp_c) / (b + dewp_c)
        return max(0.0, min(100.0, 100.0 * math.exp(gamma_d - gamma_t)))
    except (ValueError, ZeroDivisionError):
        return 50.0


def _two_hr_storm_flag(raw_data: dict) -> float:
    """1.0 if the NEA two-hour forecast flags thundery/rainy weather over any
    Changi-area town, else 0.0 (also 0.0 if the payload is missing)."""
    try:
        items = raw_data.get("two_hr_forecast", {}).get("data", {}).get("items", [])
        if not items:
            return 0.0
        for forecast in items[0].get("forecasts", []):
            area = str(forecast.get("area", ""))
            text = str(forecast.get("forecast", "")).lower()
            if area in TWO_HR_CHANGI_AREAS and any(t in text for t in _THUNDER_TOKENS):
                return 1.0
    except (KeyError, TypeError, IndexError):
        pass
    return 0.0


def _cloud_amount(clouds) -> float:
    """Sum METAR cloud layers into total cover (in eighths), plus lowest base (ft)."""
    total = 0.0
    lowest_base = 0.0
    if isinstance(clouds, list):
        for layer in clouds:
            cover = str(layer.get("cover", "FEW")).upper()
            total += _COVER_OKTA.get(cover, 2)
            base = _f(layer.get("base"), 0.0)
            if base > 0 and (lowest_base == 0 or base < lowest_base):
                lowest_base = base
    return min(8.0, total), lowest_base


def _fetch_nea_forecast_today() -> dict | None:
    """Fetch the NEA 24-hour forecast for today (SGT) and return
    {high: float, low: float} or None on any failure.

    The API returns records keyed by issue-date (up to 4/day). Each record
    has a validPeriod (start → end). We pick the most recent record whose
    validPeriod covers today's daytime window (6am–6pm SGT), then extract
    general.temperature.high/low — NEA's official expected max/min for the day.
    """
    today_sgt = datetime.now(SGT).strftime("%Y-%m-%d")
    base_url = "https://api-open.data.gov.sg/v2/real-time/api/twenty-four-hr-forecast"
    api_key = os.getenv("DATA_GOV_API_KEY", "")
    headers = {"x-api-key": api_key} if api_key else {}
    try:
        resp = requests.get(f"{base_url}?date={today_sgt}", headers=headers, timeout=8)
        resp.raise_for_status()
        records = resp.json().get("data", {}).get("records", [])
        if not records:
            return None

        # The record whose validPeriod covers today's daytime (6am–18:00).
        # Multiple records may exist for the same issue-date (morning + evening
        # updates); the latest-issued one with today in its validPeriod is best.
        # If the current 6pm→6pm record was issued today, today's daytime is
        # in the *previous* day's issue. Walk the list from newest→oldest.
        now_sgt = datetime.now(SGT)
        target_day = now_sgt.date()

        def _valid_contains_today(rec):
            vp = rec.get("general", {}).get("validPeriod", {})
            try:
                start = datetime.fromisoformat(vp.get("start", ""))
                end   = datetime.fromisoformat(vp.get("end", ""))
                # validPeriod typically midnight→midnight or 6pm→6pm.
                # Check that today's daytime (6am–6pm SGT) overlaps this window.
                today_6am  = now_sgt.replace(hour=6, minute=0, second=0, microsecond=0)
                today_6pm  = now_sgt.replace(hour=18, minute=0, second=0, microsecond=0)
                return start <= today_6am and end >= today_6pm
            except (TypeError, ValueError, AttributeError):
                return False

        # Best candidate: the most recently updated record that covers today
        # daytime; fall back to any record whose validPeriod overlaps today at
        # all (catches the midnight→midnight issuance window).
        best = None
        for rec in sorted(records, key=lambda r: r.get("updatedTimestamp", ""), reverse=True):
            if _valid_contains_today(rec):
                best = rec
                break
        if best is None:
            # Fallback: pick the latest record whose validPeriod contains right
            # now — at least that record applies to the current moment.
            for rec in sorted(records, key=lambda r: r.get("updatedTimestamp", ""), reverse=True):
                vp = rec.get("general", {}).get("validPeriod", {})
                try:
                    start = datetime.fromisoformat(vp.get("start", ""))
                    end   = datetime.fromisoformat(vp.get("end", ""))
                    if start <= now_sgt <= end:
                        best = rec
                        break
                except (TypeError, ValueError, AttributeError):
                    continue
        if best is None:
            return None

        temp = best.get("general", {}).get("temperature", {})
        high = temp.get("high")
        low  = temp.get("low")
        if high is None or low is None:
            return None
        high_f, low_f = float(high), float(low)
        # Sanity: Singapore daily max should be between 24°C and 40°C.
        if high_f < 24.0 or high_f > 40.0 or low_f < 20.0 or low_f > 35.0:
            return None
        return {"high": high_f, "low": low_f}
    except Exception:
        return None


def extract_singapore_feature_vector(raw_data: dict, metar_history: list) -> dict:
    features = {}

    # 1. WSSS METAR Features (Resolution Metric Ground Truth)
    if metar_history:
        # BUG FIX: don't assume the API returns records in a particular order -
        # sort explicitly by observation time, most recent first.
        sorted_history = sorted(
            metar_history,
            key=lambda m: _f(m.get("obsTime"), 0) if isinstance(m.get("obsTime"), (int, float)) else -1,
            reverse=True
        )

        # BUG FIX: a 24h rolling window can span into yesterday (e.g. at 2am SGT).
        # Restrict "today's max" to reports actually observed on today's SGT calendar date.
        today_sgt = datetime.now(SGT).date()
        def _safe_sgt_date(ot):
            try:
                return _sgt_date_of(ot)
            except (TypeError, ValueError, OSError):
                return None

        todays_reports = [
            m for m in sorted_history
            if "obsTime" in m and _safe_sgt_date(m["obsTime"]) == today_sgt
        ]
        # Fall back to the full window if obsTime is missing or nothing matches today yet
        # (e.g. just after midnight before the first report of the new day lands).
        reports_for_stats = todays_reports or sorted_history

        temps = [m.get("temp") for m in reports_for_stats if m.get("temp") is not None]
        latest = sorted_history[0] if sorted_history else {}

        features["wsss_current_temp"] = temps[0] if temps else latest.get("temp", 28.0)
        features["wsss_todays_max_so_far"] = max(temps) if temps else features["wsss_current_temp"]
        features["wsss_dewp"] = latest.get("dewp", 24.0)
        features["wsss_wspd"] = latest.get("wspd", 5.0)

        # ---- NEW: richer METAR-derived features (convection / moisture / trend) ----
        # Dew point depression: how far temp is above dew point. Small DPD = moist,
        # weakly-capped air -> thunderstorms likely; large DPD = dry, hot afternoon.
        dpd = _f(features["wsss_current_temp"], 28.0) - _f(features["wsss_dewp"], 24.0)
        features["wsss_dpd"] = round(max(0.0, dpd), 1)
        features["wsss_rh"] = round(
            _rh_from_dewpoint(_f(features["wsss_current_temp"], 28.0), _f(features["wsss_dewp"], 24.0)), 1
        )

        features["wsss_altim"] = _f(latest.get("altim"), 1013.0)  # pressure hPa
        features["wsss_wdir"] = _f(latest.get("wdir"), 0.0)
        features["wsss_visib_num"] = _f(str(latest.get("visib", "")).replace("+", ""), 6.0)
        features["wsss_storm_txt"] = 1.0 if any(
            t in str(latest.get("wxString", "")).lower() for t in _THUNDER_TOKENS
        ) else 0.0

        total_cloud, lowest_base = _cloud_amount(latest.get("clouds"))
        features["wsss_total_cloud_oktas"] = total_cloud
        features["wsss_low_cloud_ft"] = lowest_base

        # Pressure trend & temperature ramp over ~3h (newest -> ~3h older).
        now_epoch = _f(latest.get("obsTime"), 0.0)
        ref = now_epoch - 3 * 3600
        older = next((m for m in sorted_history if _f(m.get("obsTime"), 0.0) <= ref), None)
        features["wsss_press_trend_3h"] = round(
            features["wsss_altim"] - _f(older.get("altim") if older else None, features["wsss_altim"]), 1
        )
        features["wsss_temp_ramp_3h"] = round(
            _f(features["wsss_current_temp"], 28.0) - _f(older.get("temp") if older else None, features["wsss_current_temp"]), 1
        )

        # Risk signal: how stale is our ground-truth reading? A live prediction built on
        # a 90-minute-old METAR report is meaningfully less trustworthy than one built on
        # a 5-minute-old one - the caller widens sigma based on this.
        latest_obs_epoch = latest.get("obsTime")
        if latest_obs_epoch:
            age_seconds = (datetime.now(timezone.utc) - datetime.fromtimestamp(latest_obs_epoch, tz=timezone.utc)).total_seconds()
            features["minutes_since_last_metar"] = max(0.0, age_seconds / 60.0)
        else:
            features["minutes_since_last_metar"] = 30.0  # unknown age - assume moderately stale
    else:
        features["wsss_current_temp"] = 28.0
        features["wsss_todays_max_so_far"] = 28.0
        features["wsss_dewp"] = 24.0
        features["wsss_wspd"] = 5.0
        features["wsss_dpd"] = round(28.0 - 24.0, 1)
        features["wsss_rh"] = round(_rh_from_dewpoint(28.0, 24.0), 1)
        features["wsss_altim"] = 1013.0
        features["wsss_wdir"] = 0.0
        features["wsss_visib_num"] = 6.0
        features["wsss_storm_txt"] = 0.0
        features["wsss_total_cloud_oktas"] = 0.0
        features["wsss_low_cloud_ft"] = 0.0
        features["wsss_press_trend_3h"] = 0.0
        features["wsss_temp_ramp_3h"] = 0.0
        features["minutes_since_last_metar"] = 999.0  # no data at all - treat as maximally stale

    # 2. Spatially Weighted Air Temperature (Nearby Changi Stations)
    temp_data = raw_data.get("air_temperature", {}).get("data", {})
    readings_list = temp_data.get("readings", [])
    # BUG FIX: guard against an empty readings list before indexing [0] (was an IndexError).
    if readings_list and "stations" in temp_data:
        stations = {s["id"]: (s["location"]["latitude"], s["location"]["longitude"]) for s in temp_data.get("stations", [])}
        weighted_temp, total_weight = 0.0, 0.0

        for r in readings_list[0].get("data", []):
            st_id = r.get("stationId")
            val = r.get("value")
            if st_id in stations and val is not None:
                dist = haversine(CHANGI_LAT, CHANGI_LON, stations[st_id][0], stations[st_id][1])
                weight = 1.0 / ((dist + 0.5) ** 2)  # Inverse distance squared
                weighted_temp += val * weight
                total_weight += weight

        features["spatial_changi_prox_temp"] = weighted_temp / total_weight if total_weight > 0 else features["wsss_current_temp"]
    else:
        features["spatial_changi_prox_temp"] = features["wsss_current_temp"]

    # 3. Macro Radiation & Convection Indicators
    uv_records = raw_data.get("uv_index", {}).get("data", {}).get("records", [])
    features["uv_index"] = uv_records[0].get("value", 0) if uv_records else 0

    wbgt_records = raw_data.get("wbgt", {}).get("data", {}).get("records", [])
    features["wbgt_max"] = max([r.get("value", 0) for r in wbgt_records], default=27.0)

    lightning_data = raw_data.get("lightning", {}).get("data", {})
    features["lightning_strike_count"] = len(lightning_data.get("readings", []))

    # 4. BUG FIX: these two were expected by baseline_heuristics.py but never produced,
    # which would raise a KeyError the moment that model was actually invoked.
    features["hour_of_day"] = datetime.now(SGT).hour

    # -- Temporal / seasonal features --
    now_sgt = datetime.now(SGT)
    features["is_weekend"] = 1 if now_sgt.weekday() >= 5 else 0
    features["day_of_season"] = now_sgt.timetuple().tm_yday  # 1..366
    # Cyclic encoding of hour so 23:00 and 00:00 are close in feature space
    _hour_angle = 2.0 * math.pi * features["hour_of_day"] / 24.0
    features["hour_sin"] = round(math.sin(_hour_angle), 4)
    features["hour_cos"] = round(math.cos(_hour_angle), 4)

    rain_readings = raw_data.get("rainfall", {}).get("data", {}).get("readings", [])
    if rain_readings:
        rain_data = rain_readings[0].get("data", [])
        stations_info = raw_data.get("rainfall", {}).get("data", {}).get("stations", [])
        station_coords = {s["id"]: (s["location"]["latitude"], s["location"]["longitude"])
                          for s in stations_info}

        values = [r.get("value", 0) for r in rain_data]
        stations_total = len(values)
        stations_with_rain = sum(1 for v in values if v and v > 0)
        features["rain_station_ratio"] = (stations_with_rain / stations_total) if stations_total else 0.0
        # Rain "hotspots" = stations with non-trivial recent rain (>= ~1mm/5min).
        features["rain_hotspot_ratio"] = (
            sum(1 for v in values if _f(v, 0.0) >= _RAIN_HOTSPOT_MM) / stations_total
        ) if stations_total else 0.0

        # -- Rainfall intensity features --
        _rain_floats = [_f(v, 0.0) for v in values]
        features["rain_peak_intensity_mm"] = max(_rain_floats) if _rain_floats else 0.0
        features["rain_total_spread"] = (max(_rain_floats) - min(_rain_floats)) if len(_rain_floats) >= 2 else 0.0

        # NEW: distance from Changi to nearest heavy-rain station
        min_dist = None
        for r in rain_data:
            st_id = r.get("stationId")
            val = _f(r.get("value"), 0.0)
            if st_id in station_coords and val >= _RAIN_HOTSPOT_MM:
                lat, lon = station_coords[st_id]
                d = haversine(CHANGI_LAT, CHANGI_LON, lat, lon)
                if min_dist is None or d < min_dist:
                    min_dist = d
        features["rain_dist_to_changi_km"] = min_dist
    else:
        features["rain_station_ratio"] = 0.0
        features["rain_hotspot_ratio"] = 0.0
        features["rain_dist_to_changi_km"] = None
        features["rain_peak_intensity_mm"] = 0.0
        features["rain_total_spread"] = 0.0

    # 5. NEW: official NEA two-hour forecast - does it call thundery/rainy weather
    # for the Changi area right now? Strong max-temp suppressor, previously unused.
    features["changi_forecast_storm"] = _two_hr_storm_flag(raw_data)

    # Spatial heat concentration: island-wide max temp + spread (how hot the
    # hottest part of Singapore is, and whether heat is broadly distributed).
    temp_data = raw_data.get("air_temperature", {}).get("data", {})
    t_readings = temp_data.get("readings", [])
    if t_readings:
        t_vals = [_f(r.get("value"), None) for r in t_readings[0].get("data", [])]
        t_vals = [v for v in t_vals if v is not None]
        if t_vals:
            features["spatial_max_temp"] = round(max(t_vals), 2)
            features["spatial_temp_spread"] = round(max(t_vals) - min(t_vals), 2)

    # 6. NEA 24-hour forecast: today's official expected max/min temperature.
    #     These are the meteorologists' own best estimate of the daily max,
    #     used by the model as an informed prior instead of a fixed climatology.
    nea_fc = _fetch_nea_forecast_today()
    if nea_fc is not None:
        features["nea_forecast_high"] = nea_fc["high"]
        features["nea_forecast_low"]  = nea_fc["low"]
        features["nea_day_range"]     = round(nea_fc["high"] - nea_fc["low"], 1)
    else:
        features["nea_forecast_high"] = None
        features["nea_forecast_low"]  = None
        features["nea_day_range"]     = None

    # -- Lag / rolling features from METAR history --
    try:
        if metar_history:
            # Group observations by SGT calendar date -> list of (date, [temps])
            date_temps = defaultdict(list)
            for m in metar_history:
                ot = m.get("obsTime")
                if ot is None:
                    continue
                try:
                    d = _sgt_date_of(ot)
                    t = m.get("temp")
                    if t is not None:
                        date_temps[d].append(float(t))
                except (TypeError, ValueError):
                    continue

            sorted_dates = sorted(date_temps.keys())
            today_sgt_date = datetime.now(SGT).date()

            # Daily max for each date
            daily_max = {d: max(temps) for d, temps in date_temps.items() if temps}

            # Yesterday's max: the most recent date before today
            yesterday_dates = [d for d in sorted_dates if d < today_sgt_date]
            if yesterday_dates:
                yesterday_date = yesterday_dates[-1]
                features["yesterday_max_temp"] = daily_max[yesterday_date]
            else:
                features["yesterday_max_temp"] = None

            # 3-day rolling average: average of the 3 most recent daily maxes (before today)
            last3 = yesterday_dates[-3:] if len(yesterday_dates) >= 3 else yesterday_dates
            if last3:
                features["three_day_avg_max"] = round(
                    sum(daily_max[d] for d in last3 if d in daily_max) / len(last3), 2
                )
            else:
                features["three_day_avg_max"] = None

            # Temperature delta: current temp minus yesterday's max
            if features.get("yesterday_max_temp") is not None:
                features["temp_delta_yesterday"] = round(
                    _f(features["wsss_current_temp"], 28.0) - features["yesterday_max_temp"], 2
                )
            else:
                features["temp_delta_yesterday"] = None
        else:
            features["yesterday_max_temp"] = None
            features["three_day_avg_max"] = None
            features["temp_delta_yesterday"] = None
    except Exception:
        features["yesterday_max_temp"] = None
        features["three_day_avg_max"] = None
        features["temp_delta_yesterday"] = None

    # -- Interaction & moisture features (capture non-linear effects) --
    try:
        _rh = _f(features.get("wsss_rh"), 50.0)
        _temp = _f(features.get("wsss_current_temp"), 28.0)
        features["humidity_temp_interaction"] = round(_rh * (_temp - 26.0), 2)
    except Exception:
        features["humidity_temp_interaction"] = 0.0

    try:
        _oktas = _f(features.get("wsss_total_cloud_oktas"), 4.0)
        _uv = _f(features.get("uv_index"), 0.0)
        features["cloud_uv_interaction"] = round((8.0 - _oktas) * (_uv / 11.0), 4)
    except Exception:
        features["cloud_uv_interaction"] = 0.0

    try:
        _dpd = _f(features.get("wsss_dpd"), 4.0)
        _oktas2 = _f(features.get("wsss_total_cloud_oktas"), 4.0)
        features["dpd_cloud_interaction"] = round(_dpd * (_oktas2 / 8.0), 4)
    except Exception:
        features["dpd_cloud_interaction"] = 0.0

    return features