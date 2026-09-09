"""Tests for Polymarket integration."""

import pytest
from execution.polymarket import parse_temperature_bounds


class TestParseTemperatureBounds:
    """Test temperature bracket parsing."""

    def test_standard_bracket(self):
        """Standard bracket like '30°C' should return 29.5-30.5."""
        low, high = parse_temperature_bounds("30°C")
        assert low == 29.5
        assert high == 30.5

    def test_lower_bound(self):
        """'28°C or lower' should return -inf to 28.5."""
        low, high = parse_temperature_bounds("28°C or lower")
        assert low == float('-inf')
        assert high == 28.5

    def test_upper_bound(self):
        """'37°C or higher' should return 36.5 to inf."""
        low, high = parse_temperature_bounds("37°C or higher")
        assert low == 36.5
        assert high == float('inf')

    def test_case_insensitive(self):
        """Should handle different cases."""
        low, high = parse_temperature_bounds("30c")
        assert low == 29.5
        assert high == 30.5

    def test_with_spaces(self):
        """Should handle extra spaces."""
        low, high = parse_temperature_bounds("  30°C  ")
        assert low == 29.5
        assert high == 30.5
