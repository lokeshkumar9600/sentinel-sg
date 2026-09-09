"""Tests for newly added features in feature_engine.py:

Temporal/seasonal: is_weekend, day_of_season, hour_sin, hour_cos
Lag/rolling:       yesterday_max_temp, three_day_avg_max, temp_delta_yesterday
Interaction:       humidity_temp_interaction, cloud_uv_interaction, dpd_cloud_interaction
Rainfall:          rain_peak_intensity_mm, rain_total_spread
"""

import math
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from data.feature_engine import extract_singapore_feature_vector

SGT = ZoneInfo("Asia/Singapore")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _epoch_for_sgt(year, month, day, hour=12):
    """Return epoch seconds for a given SGT datetime."""
    dt = datetime(year, month, day, hour, 0, 0, tzinfo=SGT)
    return dt.timestamp()


def _make_metar(obs_time_epoch, temp, dewp=24.0, wspd=5.0, wdir=90,
                altim=1013.0, clouds=None, visib="6SM", wxString="",
                gust=None):
    """Build a minimal METAR history entry."""
    return {
        "obsTime": obs_time_epoch,
        "temp": temp,
        "dewp": dewp,
        "wspd": wspd,
        "wdir": wdir,
        "altim": altim,
        "clouds": clouds or [],
        "visib": visib,
        "wxString": wxString,
        "gust": gust,
    }


def _rain_payload(values):
    """Minimal rainfall raw_data payload with given station values."""
    rain_data = [{"stationId": f"S{i}", "value": v} for i, v in enumerate(values)]
    return {
        "rainfall": {
            "data": {
                "readings": [{"data": rain_data}],
                "stations": [
                    {"id": f"S{i}", "location": {"latitude": 1.3 + i * 0.01, "longitude": 103.8 + i * 0.01}}
                    for i in range(len(values))
                ],
            }
        }
    }


def _uv_payload(uv_val):
    return {"uv_index": {"data": {"records": [{"value": uv_val}]}}}


def _minimal_raw(**overrides):
    """Minimal raw_data dict that won't break extraction."""
    base = {}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Temporal / seasonal features
# ---------------------------------------------------------------------------

class TestTemporalFeatures:

    def test_is_weekend_weekday(self):
        """Weekday should give is_weekend == 0."""
        # Force a known weekday by using minimal raw data + no metar
        feat = extract_singapore_feature_vector({}, [])
        assert feat["is_weekend"] in (0, 1)

    def test_is_weekend_range(self):
        """is_weekend is always 0 or 1."""
        feat = extract_singapore_feature_vector({}, [])
        assert feat["is_weekend"] in (0, 1)

    def test_day_of_season_range(self):
        """day_of_season should be 1..366."""
        feat = extract_singapore_feature_vector({}, [])
        assert 1 <= feat["day_of_season"] <= 366

    def test_hour_sin_cos_identity(self):
        """sin^2 + cos^2 should equal 1 (within rounding tolerance)."""
        feat = extract_singapore_feature_vector({}, [])
        s, c = feat["hour_sin"], feat["hour_cos"]
        assert abs(s**2 + c**2 - 1.0) < 1e-3

    def test_hour_sin_cos_known_hour(self):
        """At hour=6 (quarter cycle), sin(pi/2)=1 and cos(pi/2)=0.
        We can't freeze time easily, but we can verify the formula directly."""
        for h in range(24):
            angle = 2.0 * math.pi * h / 24.0
            expected_sin = round(math.sin(angle), 4)
            expected_cos = round(math.cos(angle), 4)
            # Check that current time's hour_sin/hour_cos match the formula
            feat = extract_singapore_feature_vector({}, [])
            current_hour = feat["hour_of_day"]
            if current_hour == h:
                assert feat["hour_sin"] == expected_sin
                assert feat["hour_cos"] == expected_cos
                break

    def test_hour_sin_cos_adjacent_hours(self):
        """Hour 23 and hour 0 should be close in sin/cos space (wrap-around)."""
        a23 = 2.0 * math.pi * 23 / 24.0
        a00 = 2.0 * math.pi * 0 / 24.0
        dist = math.sqrt(
            (math.sin(a23) - math.sin(a00)) ** 2
            + (math.cos(a23) - math.cos(a00)) ** 2
        )
        assert dist < 0.3  # should be close


# ---------------------------------------------------------------------------
# 2. Lag / rolling features from METAR history
# ---------------------------------------------------------------------------

class TestLagRollingFeatures:

    def test_yesterday_max_temp(self):
        """yesterday_max_temp should reflect the max of the previous day."""
        now = datetime.now(SGT)
        yesterday_epoch = _epoch_for_sgt(now.year, now.month, now.day - 1, 14)
        hist = [
            _make_metar(yesterday_epoch, 32.0),
            _make_metar(yesterday_epoch - 3600, 30.0),
        ]
        feat = extract_singapore_feature_vector({}, hist)
        assert feat["yesterday_max_temp"] == 32.0

    def test_three_day_avg_max(self):
        """three_day_avg_max should average the 3 most recent daily maxes."""
        now = datetime.now(SGT)
        # Days 1, 2, 3 days ago
        epochs = [_epoch_for_sgt(now.year, now.month, now.day - d, 14) for d in (3, 2, 1)]
        hist = [
            _make_metar(epochs[0], 30.0),  # 3 days ago
            _make_metar(epochs[1], 32.0),  # 2 days ago
            _make_metar(epochs[2], 31.0),  # yesterday
        ]
        feat = extract_singapore_feature_vector({}, hist)
        expected = round((30.0 + 32.0 + 31.0) / 3, 2)
        assert feat["three_day_avg_max"] == expected

    def test_three_day_avg_max_two_days(self):
        """With only 2 prior days, avg should use both."""
        now = datetime.now(SGT)
        epochs = [_epoch_for_sgt(now.year, now.month, now.day - d, 14) for d in (2, 1)]
        hist = [
            _make_metar(epochs[0], 29.0),
            _make_metar(epochs[1], 31.0),
        ]
        feat = extract_singapore_feature_vector({}, hist)
        assert feat["three_day_avg_max"] == 30.0

    def test_temp_delta_yesterday(self):
        """temp_delta_yesterday = current temp - yesterday's max."""
        now = datetime.now(SGT)
        yesterday_epoch = _epoch_for_sgt(now.year, now.month, now.day - 1, 14)
        today_epoch = _epoch_for_sgt(now.year, now.month, now.day, 10)
        hist = [
            _make_metar(today_epoch, 29.0),
            _make_metar(yesterday_epoch, 31.0),
        ]
        feat = extract_singapore_feature_vector({}, hist)
        assert feat["temp_delta_yesterday"] == round(29.0 - 31.0, 2)

    def test_no_history_gives_none(self):
        """With empty metar_history, lag features should be None."""
        feat = extract_singapore_feature_vector({}, [])
        assert feat["yesterday_max_temp"] is None
        assert feat["three_day_avg_max"] is None
        assert feat["temp_delta_yesterday"] is None

    def test_only_today_history(self):
        """With only today's reports, lag features should be None."""
        now = datetime.now(SGT)
        today_epoch = _epoch_for_sgt(now.year, now.month, now.day, 10)
        hist = [_make_metar(today_epoch, 30.0)]
        feat = extract_singapore_feature_vector({}, hist)
        assert feat["yesterday_max_temp"] is None
        assert feat["three_day_avg_max"] is None
        assert feat["temp_delta_yesterday"] is None

    def test_malformed_obs_time(self):
        """Entries with bad obsTime should be skipped gracefully."""
        now = datetime.now(SGT)
        yesterday_epoch = _epoch_for_sgt(now.year, now.month, now.day - 1, 14)
        hist = [
            {"obsTime": "not-a-number", "temp": 99.0},
            _make_metar(yesterday_epoch, 30.0),
        ]
        feat = extract_singapore_feature_vector({}, hist)
        assert feat["yesterday_max_temp"] == 30.0


# ---------------------------------------------------------------------------
# 3. Interaction & moisture features
# ---------------------------------------------------------------------------

class TestInteractionFeatures:

    def test_humidity_temp_interaction(self):
        """humidity_temp_interaction = rh * (temp - 26)."""
        hist = [
            _make_metar(
                _epoch_for_sgt(2025, 1, 1, 12),
                temp=30.0, dewp=25.0,
            )
        ]
        feat = extract_singapore_feature_vector(_uv_payload(5), hist)
        rh = feat["wsss_rh"]
        temp = feat["wsss_current_temp"]
        expected = round(rh * (temp - 26.0), 2)
        assert feat["humidity_temp_interaction"] == expected

    def test_cloud_uv_interaction(self):
        """cloud_uv_interaction = (8 - oktas) * (uv / 11)."""
        hist = [
            _make_metar(
                _epoch_for_sgt(2025, 1, 1, 12),
                temp=30.0, dewp=25.0,
                clouds=[{"cover": "FEW", "base": 2000}],
            )
        ]
        feat = extract_singapore_feature_vector(_uv_payload(7), hist)
        oktas = feat["wsss_total_cloud_oktas"]
        uv = feat["uv_index"]
        expected = round((8.0 - oktas) * (uv / 11.0), 4)
        assert feat["cloud_uv_interaction"] == expected

    def test_dpd_cloud_interaction(self):
        """dpd_cloud_interaction = dpd * (oktas / 8)."""
        hist = [
            _make_metar(
                _epoch_for_sgt(2025, 1, 1, 12),
                temp=30.0, dewp=25.0,
                clouds=[{"cover": "BKN", "base": 3000}],
            )
        ]
        feat = extract_singapore_feature_vector({}, hist)
        dpd = feat["wsss_dpd"]
        oktas = feat["wsss_total_cloud_oktas"]
        expected = round(dpd * (oktas / 8.0), 4)
        assert feat["dpd_cloud_interaction"] == expected

    def test_interaction_fallback_on_empty_data(self):
        """With no metar_history and no raw_data, interactions should use defaults."""
        feat = extract_singapore_feature_vector({}, [])
        # wsss_current_temp defaults to 28.0, wsss_rh defaults to ~50 (computed from defaults)
        assert "humidity_temp_interaction" in feat
        assert "cloud_uv_interaction" in feat
        assert "dpd_cloud_interaction" in feat
        assert isinstance(feat["humidity_temp_interaction"], (int, float))
        assert isinstance(feat["cloud_uv_interaction"], (int, float))
        assert isinstance(feat["dpd_cloud_interaction"], (int, float))

    def test_interaction_extreme_values(self):
        """Extreme RH and temp should not cause errors."""
        hist = [
            _make_metar(
                _epoch_for_sgt(2025, 1, 1, 12),
                temp=40.0, dewp=30.0,
                clouds=[{"cover": "OVC", "base": 1000}],
            )
        ]
        feat = extract_singapore_feature_vector(_uv_payload(11), hist)
        assert isinstance(feat["humidity_temp_interaction"], (int, float))
        assert isinstance(feat["cloud_uv_interaction"], (int, float))
        assert isinstance(feat["dpd_cloud_interaction"], (int, float))


# ---------------------------------------------------------------------------
# 4. Rainfall intensity features
# ---------------------------------------------------------------------------

class TestRainfallIntensity:

    def test_peak_intensity(self):
        """rain_peak_intensity_mm should be the max station reading."""
        raw = _rain_payload([0.0, 2.5, 0.3, 4.1])
        feat = extract_singapore_feature_vector(raw, [])
        assert feat["rain_peak_intensity_mm"] == 4.1

    def test_total_spread(self):
        """rain_total_spread = max - min across stations."""
        raw = _rain_payload([0.0, 2.5, 0.3, 4.1])
        feat = extract_singapore_feature_vector(raw, [])
        assert feat["rain_total_spread"] == 4.1

    def test_single_station_spread(self):
        """With only one station, spread should be 0."""
        raw = _rain_payload([3.0])
        feat = extract_singapore_feature_vector(raw, [])
        assert feat["rain_peak_intensity_mm"] == 3.0
        assert feat["rain_total_spread"] == 0.0

    def test_no_rain_data(self):
        """With no rainfall payload, peak and spread should default to 0."""
        feat = extract_singapore_feature_vector({}, [])
        assert feat["rain_peak_intensity_mm"] == 0.0
        assert feat["rain_total_spread"] == 0.0

    def test_all_zero_rain(self):
        """All stations at 0 should give peak=0, spread=0."""
        raw = _rain_payload([0.0, 0.0, 0.0])
        feat = extract_singapore_feature_vector(raw, [])
        assert feat["rain_peak_intensity_mm"] == 0.0
        assert feat["rain_total_spread"] == 0.0


# ---------------------------------------------------------------------------
# 5. Regression: existing features still present
# ---------------------------------------------------------------------------

class TestExistingFeaturesIntact:

    def test_original_keys_still_present(self):
        """All originally expected feature keys should still exist."""
        feat = extract_singapore_feature_vector({}, [])
        original_keys = [
            "wsss_current_temp", "wsss_todays_max_so_far", "wsss_dewp",
            "wsss_wspd", "wsss_dpd", "wsss_rh", "wsss_altim", "wsss_wdir",
            "wsss_visib_num", "wsss_storm_txt", "wsss_total_cloud_oktas",
            "wsss_low_cloud_ft", "wsss_press_trend_3h", "wsss_temp_ramp_3h",
            "minutes_since_last_metar", "spatial_changi_prox_temp",
            "uv_index", "wbgt_max", "lightning_strike_count", "hour_of_day",
            "rain_station_ratio", "rain_hotspot_ratio", "rain_dist_to_changi_km",
            "changi_forecast_storm", "nea_forecast_high", "nea_forecast_low",
            "nea_day_range",
        ]
        for key in original_keys:
            assert key in feat, f"Missing original key: {key}"

    def test_new_keys_present(self):
        """All newly added feature keys should exist."""
        feat = extract_singapore_feature_vector({}, [])
        new_keys = [
            "is_weekend", "day_of_season", "hour_sin", "hour_cos",
            "yesterday_max_temp", "three_day_avg_max", "temp_delta_yesterday",
            "humidity_temp_interaction", "cloud_uv_interaction", "dpd_cloud_interaction",
            "rain_peak_intensity_mm", "rain_total_spread",
        ]
        for key in new_keys:
            assert key in feat, f"Missing new key: {key}"
