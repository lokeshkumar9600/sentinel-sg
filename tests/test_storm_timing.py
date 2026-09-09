"""Tests for storm_timing module: temporal decay and diurnal convective scaling."""

import pytest
from data.storm_timing import (
    compute_storm_timing_factor,
    refine_storm_score,
    apply_storm_history,
    _age_penalty,
    _time_of_day_penalty,
)


class TestAgePenalty:
    """Test the METAR/forecast age decay function."""

    def test_recent_metar_no_penalty(self):
        """METAR < 30 min old -> no penalty (1.0)."""
        assert _age_penalty(10.0, None) == 1.0
        assert _age_penalty(29.9, None) == 1.0

    def test_old_metar_decay(self):
        """METAR > 30 min old -> penalty < 1.0, asymptoting at 0.4."""
        penalty = _age_penalty(60.0, None)
        assert 0.4 < penalty < 1.0

    def test_very_old_metar_floors_at_0_4(self):
        """Very old METAR -> penalty approaches 0.4 asymptotically."""
        penalty = _age_penalty(10000.0, None)
        # Asymptotically approaches 0.4, not exactly equal
        assert 0.4 <= penalty < 0.41

    def test_forecast_age_penalty(self):
        """Forecast > 60 min old -> applies penalty."""
        penalty = _age_penalty(None, 120.0)
        assert 0.4 < penalty < 1.0

    def test_both_ages_combines_restrictive(self):
        """Both METAR and forecast ages -> min of penalties (more restrictive)."""
        metar_pen = _age_penalty(120.0, None)
        forecast_pen = _age_penalty(None, 120.0)
        combined = _age_penalty(120.0, 120.0)
        assert combined == min(metar_pen, forecast_pen)

    def test_none_ages_neutral(self):
        """None ages -> neutral (1.0)."""
        assert _age_penalty(None, None) == 1.0


class TestTimeOfDayPenalty:
    """Test the diurnal convective cycle scaling."""

    def test_peak_hour_is_one(self):
        """At 14:00 (peak convection) -> factor = 1.0."""
        assert _time_of_day_penalty(14.0) == 1.0

    def test_pre_dawn_minimum(self):
        """At 02:00 (12h from peak) -> factor = 0.6 (min)."""
        assert abs(_time_of_day_penalty(2.0) - 0.6) < 1e-3

    def test_symmetry_around_peak(self):
        """Hours symmetric around 14:00 should have same factor."""
        for h in range(24):
            h1 = (14 + h) % 24
            h2 = (14 - h) % 24
            assert abs(_time_of_day_penalty(h1) - _time_of_day_penalty(h2)) < 1e-6

    def test_smooth_transition(self):
        """Adjacent hours should have small differences."""
        for h in range(23):
            diff = abs(_time_of_day_penalty(h + 1) - _time_of_day_penalty(h))
            assert diff < 0.15

    def test_none_hour_neutral(self):
        """None hour -> neutral (1.0)."""
        assert _time_of_day_penalty(None) == 1.0

    def test_fractional_hours(self):
        """Fractional hours should work."""
        assert 0.6 <= _time_of_day_penalty(14.5) <= 1.0


class TestComputeStormTimingFactor:
    """Test the main compute_storm_timing_factor function."""

    def test_basic_structure(self):
        """Returns dict with expected keys."""
        features = {"minutes_since_last_metar": 10, "hour_of_day": 12}
        result = compute_storm_timing_factor(features)
        assert "storm_age_penalty" in result
        assert "time_of_day_penalty" in result
        assert "combined" in result
        assert "signals" in result

    def test_combined_is_product(self):
        """Combined = age_penalty * time_penalty."""
        features = {"minutes_since_last_metar": 10, "hour_of_day": 12}
        result = compute_storm_timing_factor(features)
        expected = result["storm_age_penalty"] * result["time_of_day_penalty"]
        assert abs(result["combined"] - expected) < 1e-4

    def test_recent_metar_no_age_penalty(self):
        """Recent METAR -> age_penalty = 1.0."""
        features = {"minutes_since_last_metar": 15, "hour_of_day": 12}
        result = compute_storm_timing_factor(features)
        assert result["storm_age_penalty"] == 1.0

    def test_old_metar_age_penalty(self):
        """Old METAR -> age_penalty < 1.0."""
        features = {"minutes_since_last_metar": 120, "hour_of_day": 12}
        result = compute_storm_timing_factor(features)
        assert result["storm_age_penalty"] < 1.0

    def test_peak_hour_no_time_penalty(self):
        """14:00 -> time_penalty = 1.0."""
        features = {"minutes_since_last_metar": 10, "hour_of_day": 14}
        result = compute_storm_timing_factor(features)
        assert result["time_of_day_penalty"] == 1.0

    def test_pre_dawn_time_penalty(self):
        """02:00 -> time_penalty = 0.6."""
        features = {"minutes_since_last_metar": 10, "hour_of_day": 2}
        result = compute_storm_timing_factor(features)
        assert abs(result["time_of_day_penalty"] - 0.6) < 1e-3

    def test_forecast_age_from_issued_at(self):
        """Uses nea_forecast_issued_at to compute forecast age."""
        import time
        features = {
            "minutes_since_last_metar": 10,
            "hour_of_day": 12,
            "nea_forecast_issued_at": time.time() - 7200,  # 2 hours ago
        }
        result = compute_storm_timing_factor(features)
        assert result["signals"]["forecast_age_min"] is not None
        assert result["signals"]["forecast_age_min"] > 100  # ~120 min

    def test_signals_debug_info(self):
        """Signals dict contains debug breakdown."""
        features = {"minutes_since_last_metar": 45, "hour_of_day": 10}
        result = compute_storm_timing_factor(features)
        sig = result["signals"]
        assert "minutes_since_metar" in sig
        assert "hour_of_day" in sig
        assert "metar_age_exceeds_threshold" in sig
        assert sig["metar_age_exceeds_threshold"] is True


class TestRefineStormScore:
    """Test the refine_storm_score function."""

    def test_no_change_when_timing_neutral(self):
        """Timing with combined=1.0 leaves score unchanged."""
        timing = {"combined": 1.0}
        assert refine_storm_score(0.5, timing) == 0.5

    def test_reduces_score(self):
        """Timing with combined < 1.0 reduces score."""
        timing = {"combined": 0.7}
        assert refine_storm_score(1.0, timing) == 0.7

    def test_clamped_to_range(self):
        """Result always in [0, 1]."""
        assert refine_storm_score(1.0, {"combined": 2.0}) == 1.0
        assert refine_storm_score(1.0, {"combined": -0.5}) == 0.0

    def test_missing_timing_neutral(self):
        """Missing timing -> neutral (1.0)."""
        assert refine_storm_score(0.5, {}) == 0.5


class TestApplyStormHistory:
    """Test the apply_storm_history alternative function."""

    def test_peak_hour_no_change(self):
        """At 14:00 -> score unchanged."""
        assert apply_storm_history(0.5, 14) == 0.5

    def test_pre_dawn_reduction(self):
        """At 02:00 -> score reduced to 60%."""
        assert abs(apply_storm_history(1.0, 2) - 0.6) < 1e-3

    def test_symmetric_around_peak(self):
        """Symmetric hours give same result."""
        for h in range(24):
            h1 = (14 + h) % 24
            h2 = (14 - h) % 24
            assert abs(apply_storm_history(1.0, h1) - apply_storm_history(1.0, h2)) < 1e-6

    def test_clamped_to_range(self):
        """Result always in [0, 1]."""
        assert apply_storm_history(2.0, 14) == 1.0
        assert apply_storm_history(-0.5, 14) == 0.0


class TestIntegration:
    """Integration-style tests mimicking real feature dicts."""

    def test_typical_morning_features(self):
        """Morning features: recent METAR, early hour -> moderate combined."""
        features = {
            "minutes_since_last_metar": 5,
            "hour_of_day": 9,
            "changi_forecast_storm": 1.0,
        }
        result = compute_storm_timing_factor(features)
        assert result["storm_age_penalty"] == 1.0
        assert 0.6 <= result["time_of_day_penalty"] <= 1.0
        assert 0.6 <= result["combined"] <= 1.0

    def test_stormy_afternoon_features(self):
        """Afternoon storm: old METAR, peak hour -> age penalty only."""
        features = {
            "minutes_since_last_metar": 90,
            "hour_of_day": 14,
            "changi_forecast_storm": 1.0,
        }
        result = compute_storm_timing_factor(features)
        assert result["storm_age_penalty"] < 1.0
        assert result["time_of_day_penalty"] == 1.0
        # Combined should be the age penalty (time penalty is 1.0)
        assert abs(result["combined"] - result["storm_age_penalty"]) < 1e-4

    def test_clear_pre_dawn(self):
        """Pre-dawn clear: recent METAR, very early -> time penalty only."""
        features = {
            "minutes_since_last_metar": 10,
            "hour_of_day": 4,
        }
        result = compute_storm_timing_factor(features)
        assert result["storm_age_penalty"] == 1.0
        assert result["time_of_day_penalty"] < 1.0
        assert abs(result["combined"] - result["time_of_day_penalty"]) < 1e-4

    def test_end_to_end_refinement(self):
        """End-to-end: raw storm score * timing factor."""
        raw_storm = 0.8  # strong raw signal
        features = {"minutes_since_last_metar": 60, "hour_of_day": 10}
        timing = compute_storm_timing_factor(features)
        refined = refine_storm_score(raw_storm, timing)
        expected = raw_storm * timing["combined"]
        assert abs(refined - expected) < 1e-4
        assert refined < raw_storm  # always reduced


if __name__ == "__main__":
    pytest.main([__file__, "-v"])