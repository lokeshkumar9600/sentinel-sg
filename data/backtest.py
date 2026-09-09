"""Backtest — reconstructs the REAL executed trades from the advisory trade
history (data/trade_history.json).

The live engine logs every actual ENTER_YES / ENTER_NO with its real fill price,
stake and edge, and every STOP / TAKE_PROFIT with the exit price and realized P&L
percentage. This module pairs each entry with its exit (FIFO per bracket+side),
recomputes the realized dollar P&L from the entry's stake, and builds the equity
curve and summary stats from those real fills — no simulated uniform-prior
pricing, no hypothetical bets.

NOTE: The market resolves using WSSS METAR daily max ONLY. All actuals
(used in per_day accuracy) are sourced from the WSSS aviation weather
station's observed temperatures — no other stations or data sources are used
for settlement.

Trades that were entered but are still open are listed as OPEN with unrealized
P&L deferred (the model doesn't hold a mark beyond the exit quote, so it is not
counted as realized).
"""

from execution.kelly_sizer import calculate_bracket_probability
from execution.polymarket import parse_temperature_bounds
from execution.trade_history import get_all_history
from data.prediction_journal import get_journal

BANKROLL = 1000.0

# Signals that open a position.
_ENTERS = frozenset({"ENTER_YES", "ENTER_NO"})
# Signals that close a position.
_EXITS = frozenset({"STOP", "TAKE_PROFIT", "RESOLVED_OR_STALE", "RESOLVED"})


def _side(signal: str) -> str:
    return signal.removeprefix("ENTER_")  # ENTER_YES -> YES, ENTER_NO -> NO


def _reconstruct_positions(history: list) -> tuple[list, list, int]:
    """Pair each ENTER with its subsequent exit per (bracket|side).

    Returns (closed_positions, open_positions, unpaired_exits). FIFO-ordered so
    several entries into the same bracket+side resolve in the order they opened.
    """
    queue: dict[str, list] = {}          # bracket|side -> list of open entries
    closed: list[dict] = []
    open_pos: list[dict] = []

    for ev in history:
        signal = ev.get("signal") or ""
        bracket = ev.get("bracket")
        if not bracket:
            continue
        ts = ev.get("timestamp_sgt") or ""

        if signal in _ENTERS:
            key = f"{bracket}|{_side(signal)}"
            queue.setdefault(key, []).append({
                "bracket": bracket,
                "side": _side(signal),
                "entry_price": ev.get("entry_price"),
                "stake_usd": float(ev.get("stake_usd") or 0.0),
                "edge": float(ev.get("edge") or 0.0),
                "entry_at": ts,
            })

        elif signal in _EXITS:
            side = ev.get("side") or "YES"
            key = f"{bracket}|{side}"
            open_entries = queue.get(key)
            if not open_entries:
                # Exit with no matching entry in this window (old session) —
                # count it, but there is no stake to attach.
                closed.append({
                    "bracket": bracket, "side": side,
                    "exit_price": ev.get("exit_price"), "pnl_pct": float(ev.get("pnl_pct") or 0.0),
                    "exit_at": ts, "exit_signal": signal,
                    "reason": ev.get("reason") or "", "orphan": True,
                    "stake_usd": 0.0, "edge": 0.0, "entry_price": None,
                })
                continue
            ent = open_entries.pop(0)
            closed.append({
                "bracket": ent["bracket"], "side": ent["side"],
                "entry_price": ent["entry_price"],
                "stake_usd": ent["stake_usd"],
                "edge": ent["edge"],
                "exit_price": ev.get("exit_price"),
                "pnl_pct": float(ev.get("pnl_pct") or 0.0),
                "entry_at": ent["entry_at"],
                "exit_at": ts,
                "exit_signal": signal,
                "reason": ev.get("reason") or "",
            })

    # Any entries still open at the end of the log are held positions.
    for lst in queue.values():
        open_pos.extend(lst)

    unpaired_exits = sum(len(v) for v in queue.values())
    return closed, open_pos, unpaired_exits


def _per_day_accuracy(trade_dates: set[str] | None = None) -> list[dict]:
    """Per-day Prediction vs Actual from the prediction journal.

    Each day the model recorded (mu, sigma) and, once settled, the actual WSSS
    max. P(bracket) is the model's own assigned probability that the actual max
    fell inside the temperature bucket it landed in — i.e. how much confidence
    the model had in the bracket that actually occurred. Unsettled days show
    '—' rather than NaN.

    If trade_dates is provided, each row gets a 'trade_status' field:
      'traded' — the model entered a position on this date
      'no_trade' — the model saw the day but chose not to trade (all signals were SKIP/TIMING_HOLD)
    """
    rows = []
    for e in get_journal():  # newest first
        mu = e.get("predicted_mu")
        sigma = e.get("predicted_sigma")
        actual = e.get("actual_max")
        row = {
            "date": e.get("date_str", ""),
            "hour_of_day": e.get("hour_of_day"),
            "predicted_mu": mu,
            "predicted_sigma": sigma,
            "actual_max": actual,
            "error": None,
            "p_bracket": None,
        }
        if mu is not None and sigma is not None and actual is not None:
            low, high = parse_temperature_bounds(f"{round(actual)}°C")
            row["p_bracket"] = round(
                calculate_bracket_probability(low, high, mu, sigma), 4
            )
            row["error"] = round(actual - mu, 2)
        rows.append(row)

    # Annotate trade status if caller provided trade dates
    if trade_dates is not None:
        for row in rows:
            d = row["date"]
            # Convert "September-05-2026" to a comparable format
            date_key = _date_str_to_key(d)
            row["trade_status"] = "traded" if date_key in trade_dates else "no_trade"

    return rows


def _date_str_to_key(date_str: str) -> str:
    """Convert any date format to 'YYYY-MM-DD'.

    Handles:
      'September-05-2026' (journal format) → '2026-09-05'
      '2026-09-05' (raw timestamp format)  → '2026-09-05'
      '2026-09-05 14:30:00 SGT'            → '2026-09-05'
    """
    if not date_str:
        return ""
    # Already YYYY-MM-DD (possibly with time suffix)
    raw = date_str.strip()[:10]
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return raw
    # Journal format: "September-05-2026"
    parts = date_str.split("-")
    if len(parts) == 3:
        month_map = {
            "January": "01", "February": "02", "March": "03", "April": "04",
            "May": "05", "June": "06", "July": "07", "August": "08",
            "September": "09", "October": "10", "November": "11", "December": "12",
        }
        mm = month_map.get(parts[0], "?")
        dd = parts[1].zfill(2)
        yyyy = parts[2]
        return f"{yyyy}-{mm}-{dd}"
    return date_str[:10]


def run_backtest() -> dict:
    history = get_all_history()
    closed, open_pos, _ = _reconstruct_positions(history)

    trades: list[dict] = []
    equity = BANKROLL
    curve = [{"date": "start", "equity": round(equity, 2)}]

    for p in closed:
        stake = p["stake_usd"]
        # Realized USD for a position bought at entry_rate and closed at exit_rate:
        # shares = stake/entry, proceeds = shares*exit => pnl = stake*(exit-entry)/entry.
        pnl = stake * p["pnl_pct"]
        equity += pnl
        win = p["pnl_pct"] > 0  # a take-profit; STOPs are pnl <= 0
        trades.append({
            "date": (p["entry_at"] or "")[:10],
            "entry_at": p["entry_at"],
            "exit_at": p["exit_at"],
            "bracket": p["bracket"],
            "side": p["side"],
            "entry_price": p["entry_price"],
            "exit_price": p["exit_price"],
            "edge": round(p["edge"], 3),
            "stake": round(stake, 2),
            "pnl_pct": round(p["pnl_pct"], 4),
            "action": "WIN" if win else "LOSS",
            "exit_signal": p["exit_signal"],
            "win": bool(win),
            "pnl": round(pnl, 2),
            "equity": round(equity, 2),
            "orphan": p.get("orphan", False),
            "reason": p.get("reason", ""),
        })
        curve.append({
            "date": (p["exit_at"] or p["entry_at"] or "").split(" ")[0][5:],
            "equity": round(equity, 2),
        })

    # Open (held) positions are listed but contribute no realized P&L.
    for p in open_pos:
        trades.append({
            "date": (p["entry_at"] or "")[:10],
            "entry_at": p["entry_at"],
            "exit_at": None,
            "bracket": p["bracket"],
            "side": p["side"],
            "entry_price": p["entry_price"],
            "exit_price": None,
            "edge": round(p["edge"], 3),
            "stake": round(p["stake_usd"], 2),
            "pnl_pct": None,
            "action": "OPEN",
            "exit_signal": None,
            "win": None,
            "pnl": None,
            "equity": round(equity, 2),
            "is_open": True,
        })

    # Keep the table newest-first like /api/history.
    trades.sort(key=lambda t: t["entry_at"] or t["exit_at"] or "", reverse=True)

    played = [t for t in trades if t.get("win") is not None]
    closed_list = [t for t in trades if t.get("win") is not None]
    wins = sum(1 for t in played if t["win"])
    losses = len(played) - wins
    gross_profit = sum(t["pnl"] for t in closed_list if t["pnl"] > 0)
    gross_loss = -sum(t["pnl"] for t in closed_list if t["pnl"] < 0)
    total_pnl = sum(t["pnl"] for t in closed_list)

    # Collect dates that had actual trades (any signal, not just closed)
    dates_with_signals = set()
    for h in get_all_history():
        sig = (h.get("signal") or "").upper()
        if sig.startswith("ENTER_"):
            dates_with_signals.add(_date_str_to_key((h.get("timestamp_sgt") or "")[:10]))

    return {
        "bankroll": BANKROLL,
        "assumption": "reconstructed from the live advisory trade history "
                      "(data/trade_history.json): actual fills and their recorded "
                      "stops / take-profits — no simulated pricing",
        "days_settled": len({t["date"] for t in closed_list}),
        "days_tracked": len(dates_with_signals),
        "trades_taken": len(played),
        "open_positions": len(open_pos),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(played), 4) if played else None,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else None,
        "total_pnl": round(total_pnl, 2),
        "avg_edge": round(sum(t["edge"] for t in played) / len(played), 3) if played else 0.0,
        "final_equity": round(equity, 2),
        "return_pct": round((equity - BANKROLL) / BANKROLL * 100.0, 2),
        "curve": curve,
        "trades": trades,
        "per_day": _per_day_accuracy(dates_with_signals),
    }