"""Tests for advisory position book."""

import time
import pytest
from execution.positions import PositionBook, HOLD, TAKE_PROFIT, STOP
from data.config import TAKE_PROFIT_PCT, STOP_LOSS_PCT


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
