"""Trade history log — records every entry, exit, and signal recommendation with timestamps.

Each entry is appended to a local JSON file so the history persists across restarts.
"""

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SGT = ZoneInfo("Asia/Singapore")
HISTORY_FILE = Path("data/trade_history.json")


def _load() -> list:
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    return []


def _save(history: list):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


# Thread-safe append
_lock = threading.Lock()


def log_signal(
    signal_type: str,  # ENTER_YES, ENTER_NO, TAKE_PROFIT, STOP, TIMING_HOLD, SKIP, NO_TRADE
    bracket: str,
    side: str | None,
    entry_price: float | None,
    exit_price: float | None,
    stake_usd: float,
    edge: float,
    pnl_pct: float | None,
    reason: str = "",
) -> None:
    """Append a signal or trade event to the history log."""
    with _lock:
        history = _load()
        entry = {
            "timestamp_sgt": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT"),
            "signal": signal_type,
            "bracket": bracket,
            "side": side,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "stake_usd": stake_usd,
            "edge": edge,
            "pnl_pct": pnl_pct,
            "reason": reason,
        }
        history.append(entry)
        _save(history)


def log_entry(bracket: str, side: str, entry_price: float, stake_usd: float, edge: float) -> None:
    log_signal(f"ENTER_{side}", bracket, side, entry_price, None, stake_usd, edge, None, "")


def log_exit(bracket: str, side: str, exit_price: float, pnl_pct: float, reason: str = "") -> None:
    # Map exit reason to signal
    signal = "TAKE_PROFIT" if pnl_pct >= 0 else "STOP"
    log_signal(signal, bracket, side, None, exit_price, 0.0, 0.0, pnl_pct, reason)


def log_signal_only(signal: str, bracket: str, edge: float, reason: str = "") -> None:
    log_signal(signal, bracket, None, None, None, 0.0, edge, None, reason)


def get_history(limit: int = 50) -> list:
    """Return the most recent `limit` signals, newest first."""
    with _lock:
        history = _load()
    return history[-limit:][::-1]  # newest first


def clear_history() -> None:
    """Wipe all history (e.g. at start of new trading day)."""
    with _lock:
        _save([])