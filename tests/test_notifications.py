"""Tests for the webhook notification system."""

import importlib
import os

import pytest

os.environ["TRADE_WEBHOOK_URL"] = "https://ntfy.sh/test-id"
import execution.notifications as notifications  # noqa: E402

importlib.reload(notifications)


class TestEnabled:
    def test_enabled_with_webhook(self):
        assert notifications.is_enabled() is True


class TestPayload:
    def test_contains_signal_and_bracket(self):
        p = notifications._build_payload({
            "signal": "ENTER_YES",
            "bracket": "33°C",
            "side": "YES",
            "stake_usd": 1.5,
            "edge": 0.03,
            "entry_price": 0.62,
        })
        assert "ENTER_YES" in p["message"]
        assert "33°C" in p["message"]
        assert "1.50" in p["message"]  # stake dollars

    def test_exit_payload_includes_pnl(self):
        p = notifications._build_payload({
            "signal": "TAKE_PROFIT",
            "bracket": "32°C",
            "side": "NO",
            "entry_price": 0.40,
            "exit_price": 0.68,
            "pnl_pct": 0.70,
        })
        assert "+70.00%" in p["message"]
        assert "0.68" in p["message"]


class TestDedupe:
    def test_first_send_then_suppress(self):
        notifications._last_sent = {}
        e = {"signal": "STOP", "bracket": "32°C", "side": "YES"}
        assert notifications._should_send(e) is True
        assert notifications._should_send(e) is False

    def test_different_side_not_suppressed(self):
        notifications._last_sent = {}
        yes = {"signal": "STOP", "bracket": "32°C", "side": "YES"}
        no = {"signal": "STOP", "bracket": "32°C", "side": "NO"}
        assert notifications._should_send(yes) is True
        assert notifications._should_send(no) is True

    def test_different_bracket_not_suppressed(self):
        notifications._last_sent = {}
        a = {"signal": "ENTER_YES", "bracket": "33°C", "side": "YES"}
        b = {"signal": "ENTER_YES", "bracket": "32°C", "side": "YES"}
        assert notifications._should_send(a) is True
        assert notifications._should_send(b) is True


class TestNotifyMuted:
    def test_unset_webhook_mutes(self):
        old = os.environ.get("TRADE_WEBHOOK_URL")
        os.environ["TRADE_WEBHOOK_URL"] = ""
        try:
            assert notifications.notify({"signal": "ENTER_YES", "bracket": "X"}) is None
        finally:
            if old:
                os.environ["TRADE_WEBHOOK_URL"] = old
            else:
                del os.environ["TRADE_WEBHOOK_URL"]
        importlib.reload(notifications)