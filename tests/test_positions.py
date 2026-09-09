"""Tests for advisory position book."""

import time
import pytest
from execution.positions import PositionBook, HOLD, TAKE_PROFIT, STOP
from data.config import TAKE_PROFIT_PCT, STOP_LOSS_PCT, TAKE_PROFIT_SIGMA_SCALE


class TestPositionBook:
    """Test position book operations."""

    def test_enter_position(self):
        """Should successfully enter a new position."""
        book = PositionBook()
        entered = book.enter(
            bracket="30°C",
            side="YES",
            entry_price=0.50,
            stake_usd=1.0,
            model_prob=0.6,
            edge=0.1,
        )
        assert entered is True
        assert len(book) == 1

    def test_no_double_entry(self):
        """Should not allow double entry on same bracket+side."""
        book = PositionBook()
        book.enter("30°C", "YES", 0.50, 1.0, 0.6, 0.1)
        entered = book.enter("30°C", "YES", 0.50, 1.0, 0.6, 0.1)
        assert entered is False
        assert len(book) == 1

    def test_multiple_brackets(self):
        """Should allow different brackets."""
        book = PositionBook()
        book.enter("30°C", "YES", 0.50, 1.0, 0.6, 0.1)
        book.enter("31°C", "YES", 0.30, 1.0, 0.4, 0.1)
        assert len(book) == 2

    def test_has_position(self):
        """Should correctly report position existence."""
        book = PositionBook()
        assert book.has("30°C", "YES") is False
        book.enter("30°C", "YES", 0.50, 1.0, 0.6, 0.1)
        assert book.has("30°C", "YES") is True
        assert book.has("30°C", "NO") is False

    def test_update_prices_take_profit(self):
        """Should trigger TAKE_PROFIT when price rises above threshold."""
        book = PositionBook()
        book.enter("30°C", "YES", 0.50, 1.0, 0.6, 0.1)

        # Price rises to take-profit level
        book.update_prices({"30°C|YES": 0.51})  # 2% gain
        snapshot = book.snapshot()
        assert snapshot[0]["action"] == TAKE_PROFIT

    def test_update_prices_stop_loss(self):
        """Should trigger STOP when price falls below threshold."""
        book = PositionBook()
        book.enter("30°C", "YES", 0.50, 1.0, 0.6, 0.1)

        # Price falls to stop-loss level
        book.update_prices({"30°C|YES": 0.49})  # -2% loss
        snapshot = book.snapshot()
        assert snapshot[0]["action"] == STOP

    def test_settle_actions(self):
        """Should remove settled positions from book."""
        book = PositionBook()
        book.enter("30°C", "YES", 0.50, 1.0, 0.6, 0.1)

        # Trigger stop
        book.update_prices({"30°C|YES": 0.49})
        closed = book.settle_actions()

        assert len(closed) == 1
        assert closed[0]["action"] == STOP
        assert len(book) == 0

    def test_snapshot_returns_copy(self):
        """Snapshot should return a copy, not the internal dict."""
        book = PositionBook()
        book.enter("30°C", "YES", 0.50, 1.0, 0.6, 0.1)

        snap1 = book.snapshot()
        snap2 = book.snapshot()

        # Modifying one shouldn't affect the other
        snap1[0]["bracket"] = "MODIFIED"
        assert snap2[0]["bracket"] == "30°C"

    def test_clear(self):
        """Should remove all positions."""
        book = PositionBook()
        book.enter("30°C", "YES", 0.50, 1.0, 0.6, 0.1)
        book.enter("31°C", "NO", 0.30, 1.0, 0.4, 0.1)
        book.clear()
        assert len(book) == 0


class TestCooldown:
    """Anti-churn cooldown: a STOP blocks re-entry for a cooling-off period."""

    def test_stop_blocks_reentry_within_cooldown(self):
        book = PositionBook()
        assert book.enter("33°C", "YES", 0.50, 1.0, 0.6, 0.03) is True
        # Drop the exit quote -> STOP, then settle (records the stop time).
        book.update_prices({"33°C|YES": 0.30})
        assert book.snapshot()[0]["action"] == STOP
        closed = book.settle_actions()
        assert len(closed) == 1
        # Immediate re-entry is refused by the cooldown.
        assert book.enter("33°C", "YES", 0.40, 1.0, 0.6, 0.02) is False

    def test_reentry_allowed_after_cooldown_expires(self):
        book = PositionBook()
        assert book.enter("33°C", "YES", 0.50, 1.0, 0.6, 0.03) is True
        book.update_prices({"33°C|YES": 0.30})
        book.settle_actions()
        assert book.enter("33°C", "YES", 0.40, 1.0, 0.6, 0.02) is False
        # Fudge the cooldown timestamp back past the window.
        book._cooldowns["33°C|YES"] = time.time() - 10000
        assert book.enter("33°C", "YES", 0.40, 1.0, 0.6, 0.02) is True

    def test_take_profit_does_not_trigger_cooldown(self):
        book = PositionBook()
        assert book.enter("32°C", "NO", 0.40, 1.0, 0.6, 0.05) is True
        # Exit quote far above entry -> TAKE_PROFIT; no cooldown recorded.
        book.update_prices({"32°C|NO": 0.80})
        closed = book.settle_actions()
        assert len(closed) == 1 and closed[0]["action"] == TAKE_PROFIT
        assert "32°C|NO" not in book._cooldowns
        assert book.enter("32°C", "NO", 0.40, 1.0, 0.6, 0.05) is True


class TestSigmaAwareProfitBand:
    """Test sigma-aware take-profit widening."""

    def test_take_profit_default_unchanged(self):
        """With default TAKE_PROFIT_SIGMA_SCALE=0.0, behavior is unchanged."""
        book = PositionBook()
        book.enter("30°C", "YES", 0.50, 1.0, 0.6, 0.1, model_sigma=0.8)

        # 1% gain should trigger take-profit (base TAKE_PROFIT_PCT = 0.01)
        book.update_prices({"30°C|YES": 0.505})  # 1% gain
        assert book.snapshot()[0]["action"] == TAKE_PROFIT

    def test_take_profit_widens_with_sigma(self):
        """When TAKE_PROFIT_SIGMA_SCALE > 0, take-profit widens with sigma."""
        # Temporarily modify the config for this test
        import data.config as config
        original_scale = config.TAKE_PROFIT_SIGMA_SCALE
        config.TAKE_PROFIT_SIGMA_SCALE = 0.01  # 1% per sigma unit

        try:
            book = PositionBook()
            # High sigma day (0.8) -> take_profit = 0.01 + 0.01*0.8 = 0.018 (1.8%)
            book.enter("30°C", "YES", 0.50, 1.0, 0.6, 0.1, model_sigma=0.8)

            # 1.5% gain should NOT trigger (need 1.8%)
            book.update_prices({"30°C|YES": 0.5075})  # 1.5% gain
            assert book.snapshot()[0]["action"] == HOLD

            # 2% gain should trigger
            book.update_prices({"30°C|YES": 0.51})  # 2% gain
            assert book.snapshot()[0]["action"] == TAKE_PROFIT
        finally:
            config.TAKE_PROFIT_SIGMA_SCALE = original_scale

    def test_take_profit_sigma_scale_constant(self):
        """Verify the constant exists and defaults to 0.0."""
        assert TAKE_PROFIT_SIGMA_SCALE == 0.0
