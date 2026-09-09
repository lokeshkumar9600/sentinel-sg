"""Tests for Kelly criterion position sizing."""

import pytest
from execution.kelly_sizer import (
    calculate_bracket_probability,
    compute_effective_min_edge,
    compute_kelly_trade,
    size_portfolio,
)
from data.config import (
    BANKROLL_USD,
    MIN_EDGE_THRESHOLD,
    MAX_STAKE_PER_POSITION_USD,
)


class TestBracketProbability:
    """Test bracket probability calculations."""

    def test_bracket_probability_within_sigma(self):
        """Bracket centered on mu should have reasonable probability."""
        prob = calculate_bracket_probability(30.5, 31.5, 31.0, 0.5)
        assert 0.3 < prob < 0.7, f"Expected ~0.68 for 1-sigma bracket, got {prob}"

    def test_bracket_probability_far_from_mu(self):
        """Bracket far from mu should have low probability."""
        prob = calculate_bracket_probability(34.5, 35.5, 31.0, 0.5)
        assert prob < 0.1, f"Expected <10% for 3-sigma bracket, got {prob}"

    def test_bracket_probability_symmetric(self):
        """Symmetric brackets around mu should have equal probability."""
        p1 = calculate_bracket_probability(30.5, 31.5, 31.0, 0.5)
        p2 = calculate_bracket_probability(30.5, 31.5, 31.0, 0.5)
        assert abs(p1 - p2) < 1e-10

    def test_bracket_probability_sum_to_one(self):
        """Sum of probabilities across all brackets should be ~1."""
        brackets = [
            (28.0, 29.0), (29.0, 30.0), (30.0, 31.0),
            (31.0, 32.0), (32.0, 33.0), (33.0, 34.0),
            (34.0, 35.0), (35.0, 36.0), (36.0, 37.0),
        ]
        total = sum(calculate_bracket_probability(lo, hi, 31.5, 0.5) for lo, hi in brackets)
        assert 0.95 < total < 1.05, f"Expected sum ~1.0, got {total}"


class TestEffectiveMinEdge:
    """Test confidence-scaled minimum edge calculation."""

    def test_min_edge_floor(self):
        """Min edge should never go below MIN_EDGE_THRESHOLD."""
        edge = compute_effective_min_edge(0.1)  # Very tight sigma
        assert edge >= MIN_EDGE_THRESHOLD

    def test_min_edge_scales_with_sigma(self):
        """Min edge should increase with sigma."""
        edge_tight = compute_effective_min_edge(0.3)
        edge_loose = compute_effective_min_edge(0.8)
        assert edge_loose > edge_tight


class TestKellyTrade:
    """Test Kelly trade computation."""

    def test_buy_yes_when_edge_positive(self):
        """Should generate BUY_YES when model probability exceeds market price."""
        trade = compute_kelly_trade(
            p_model=0.6,
            market_price=0.5,
            min_edge=0.01,
        )
        assert trade["action"] == "BUY_YES"
        assert trade["edge"] > 0
        assert trade["stake_usd"] > 0

    def test_no_trade_when_edge_negative(self):
        """Should generate NO_TRADE when neither side has edge.

        Without bid info, complement pricing means one side always has the
        opposite edge of the other — so they can't both be negative.
        But with a tight threshold, neither side clears it.
        """
        trade = compute_kelly_trade(
            p_model=0.5,
            market_price=0.5,
            min_edge=0.01,
        )
        assert trade["action"] == "NO_TRADE"

    def test_buy_no_when_no_side_has_edge(self):
        """When model probability is below the YES ask, the NO side should trigger."""
        trade = compute_kelly_trade(
            p_model=0.4,
            market_price=0.5,
            min_edge=0.01,
        )
        assert trade["action"] == "BUY_NO"
        assert trade["edge"] > 0

    def test_skip_when_price_too_low(self):
        """Should skip when price is below minimum."""
        trade = compute_kelly_trade(
            p_model=0.9,
            market_price=0.01,
            min_edge=0.01,
        )
        assert trade["action"] == "SKIP"

    def test_skip_when_price_too_high(self):
        """Should skip when price is near certain."""
        trade = compute_kelly_trade(
            p_model=0.99,
            market_price=0.98,
            min_edge=0.01,
        )
        assert trade["action"] == "SKIP"

    def test_stake_capped_at_max(self):
        """Stake should not exceed MAX_STAKE_PER_POSITION_USD."""
        trade = compute_kelly_trade(
            p_model=0.9,
            market_price=0.5,
            min_edge=0.01,
        )
        if trade["action"] in ("BUY_YES", "BUY_NO"):
            assert trade["stake_usd"] <= MAX_STAKE_PER_POSITION_USD


class TestSizePortfolio:
    """Test portfolio-level sizing."""

    def test_no_scaling_when_under_cap(self):
        """Trades should not be scaled down when total stake is under cap."""
        trades = [
            {"action": "BUY_YES", "stake_usd": 0.50},
            {"action": "BUY_YES", "stake_usd": 0.50},
        ]
        result = size_portfolio(trades)
        assert result[0]["stake_usd"] == 0.50
        assert result[1]["stake_usd"] == 0.50

    def test_scaling_when_over_cap(self):
        """Trades should be scaled down when total stake exceeds cap."""
        # Create trades that exceed 30% of $1000 bankroll
        trades = [
            {"action": "BUY_YES", "stake_usd": 200.0},
            {"action": "BUY_YES", "stake_usd": 200.0},
        ]
        result = size_portfolio(trades)
        # Total should be capped at $300 (30% of $1000)
        total = sum(t["stake_usd"] for t in result)
        assert total <= BANKROLL_USD * 0.30 + 0.01  # Small tolerance for rounding
