"""Tests for feature engineering."""

import pytest
from data.feature_engine import _rh_from_dewpoint, haversine
from main import _diurnal_heating_fraction, _convection_storm_score


class TestRelativeHumidity:
    """Test relative humidity calculation from dewpoint."""

    def test_rh_at_dewpoint(self):
        """RH should be 100% when temp equals dewpoint."""
        rh = _rh_from_dewpoint(28.0, 28.0)
        assert abs(rh - 100.0) < 0.1

    def test_rh_below_dewpoint(self):
        """RH should be less than 100% when temp > dewpoint."""
        rh = _rh_from_dewpoint(30.0, 25.0)
        assert 50 < rh < 80

    def test_rh_bounded(self):
        """RH should always be between 0 and 100."""
        rh = _rh_from_dewpoint(35.0, 20.0)
        assert 0 <= rh <= 100


class TestHaversine:
    """Test haversine distance calculation."""

    def test_same_point(self):
        """Distance to same point should be 0."""
        d = haversine(1.352, 103.82, 1.352, 103.82)
        assert d < 0.01

    def test_known_distance(self):
        """Distance between two known points should be reasonable."""
        # Singapore to KL is ~300km
        d = haversine(1.352, 103.82, 3.139, 101.687)
        assert 280 < d < 320

    def test_short_distance(self):
        """Short distances should be accurate."""
        # ~1km apart
        d = haversine(1.352, 103.82, 1.361, 103.82)
        assert 0.9 < d < 1.1


class TestDiurnalHeatingFraction:
    """Test the diurnal heating curve."""

    def test_overnight_low(self):
        """Pre-dawn hours should be near 0."""
        assert _diurnal_heating_fraction(3) == 0.05

    def test_afternoon_complete(self):
        """After the daily peak, heating should be done."""
        assert _diurnal_heating_fraction(16) == 1.0

    def test_monotonic_increasing(self):
        """Heating fraction should increase through the morning."""
        vals = [_diurnal_heating_fraction(h) for h in (7, 9, 11, 13, 14)]
        assert vals == sorted(vals)


class TestConvectionStormScore:
    """Test storm suppression scoring."""

    def test_no_storm_sources(self):
        """No storm signals should give a low score."""
        assert _convection_storm_score({}) == 0.0

    def test_forecast_storm(self):
        """A thundery forecast should push the score up."""
        feats = {"changi_forecast_storm": True}
        assert _convection_storm_score(feats) >= 0.35

    def test_score_bounded(self):
        """Score should never exceed 1.0."""
        feats = {
            "changi_forecast_storm": True,
            "wsss_storm_txt": 1.0,
            "lightning_strike_count": 50,
            "rain_station_ratio": 1.0,
            "rain_dist_to_changi_km": 0.0,
        }
        assert 0.0 <= _convection_storm_score(feats) <= 1.0
