"""Advisory position book for the SG max-temp bracket market.

This system cannot place real Polymarket orders — it is decision support. This
book tracks WHAT the model would have entered, at what price, and uses the live
SELL prices (yes_sell / no_sell from the WS feed) to compute a running P&L and
emit an exit action. It is a simulation of a hold-to-take-profit / stop-loss
book so the dashboard can show when to get out, not just when to get in.

Thread-safety: the WS feed thread and the FastAPI request threads both touch
the book, so every access goes through a lock (mirrors LiveFeed).
"""

import threading
import time

from data.config import STOP_LOSS_PCT, TAKE_PROFIT_PCT

# Actions a position can be in.
HOLD = "HOLD"
TAKE_PROFIT = "TAKE_PROFIT"
STOP = "STOP"
RESOLVED_OR_STALE = "RESOLVED_OR_STALE"


class PositionBook:
    """In-memory, thread-safe map of open bracket positions.

    Each position is keyed by the bracket title + side (YES/NO). Because the
    brackets are mutually exclusive outcomes of one event, holding YES on a
    bracket is offset exactly by the negRisk book; we track both sides so a NO
    entry on 29C (a portfolio of the other YESes) manages itself the same way.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._book: dict[str, dict] = {}

    def _key(self, bracket: str, side: str) -> str:
        return f"{bracket}|{side}"

    # ---- public API --------------------------------------------------------

    def enter(self, bracket: str, side: str, entry_price: float, stake_usd: float,
              model_prob: float, edge: float, hour_of_day: float | None = None,
              replace: bool = False) -> bool:
        """Open (or replace) an entry for a bracket+side. Returns True if entered.

        `entry_price` is the BUY price paid (YES ask, or NO's negRisk price).
        One position per bracket+side; unless replace=True, an existing entry is
        kept and the new one is refused (no doubling down).
        """
        key = self._key(bracket, side)
        now = time.time()
        with self._lock:
            if not replace and key in self._book:
                return False
            self._book[key] = {
                "bracket": bracket,
                "side": side,
                "entry_price": entry_price,
                "stake_usd": stake_usd,
                "model_prob": model_prob,
                "edge": edge,
                "hour_of_day": hour_of_day,
                "entry_at": now,
                "exit_price": None,
                "pnl_pct": 0.0,
                "action": HOLD,
            }
            return True

    def has(self, bracket: str, side: str) -> bool:
        with self._lock:
            return self._key(bracket, side) in self._book

    def update_prices(self, sell_prices: dict):
        """Feed the latest sell price per bracket+side, recompute P&L + action.

        `sell_prices` maps a key f"{bracket}|{side}" -> price (the price at which
        you could currently close: YES sell = best bid; NO sell = no_sell).
        """
        with self._lock:
            now_epoch = time.time()
            for key, pos in self._book.items():
                price = sell_prices.get(key)
                if price is None:
                    # No exit quote right now - can't manage the risk; keep prior
                    # P&L and keep holding rather than guessing a mark.
                    continue
                if price <= 0:
                    continue
                pos["exit_price"] = price
                pos["pnl_pct"] = (price - pos["entry_price"]) / pos["entry_price"]
                if pos["pnl_pct"] >= TAKE_PROFIT_PCT:
                    pos["action"] = TAKE_PROFIT
                    pos["action_at"] = now_epoch
                elif pos["pnl_pct"] <= -STOP_LOSS_PCT:
                    pos["action"] = STOP
                    pos["action_at"] = now_epoch
                else:
                    pos["action"] = HOLD

    def mark_resolved(self, keys):
        """Mark a set of positions (keys) as RESOLVED_OR_STALE - e.g. the event
        closed or the bracket's book froze - so they get settled and removed."""
        with self._lock:
            for key in keys:
                if key in self._book:
                    self._book[key]["action"] = RESOLVED_OR_STALE
                    self._book[key]["action_at"] = time.time()

    def settle_actions(self) -> list[dict]:
        """Return any positions currently in an exit action (TAKE_PROFIT/STOP/
        RESOLVED_OR_STALE) and REMOVE them from the book (advisory auto-close).
        Returns the closed positions so the caller can surface them once."""
        closed = []
        with self._lock:
            for key, pos in list(self._book.items()):
                if pos["action"] in (TAKE_PROFIT, STOP, RESOLVED_OR_STALE):
                    closed.append({**pos, "closed_action": pos["action"]})
                    del self._book[key]
        return closed

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [
                {**pos} for pos in sorted(
                    self._book.values(), key=lambda p: p.get("entry_at", 0)
                )
            ]

    def clear(self):
        with self._lock:
            self._book.clear()

    def __len__(self):
        with self._lock:
            return len(self._book)
