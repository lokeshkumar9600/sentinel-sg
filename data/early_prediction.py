"""Early-prediction analysis — how soon the model calls the right bracket.

Instead of "how accurate is the model by end-of-day?" (trivially easy for max
temperature), this asks the practical trading question: how soon does the
model's bracket prediction first become correct, stable and tradeable — and does
it hold from then on?

NOTE: The market resolves using WSSS METAR daily max ONLY. All "actual" values
used here come from the WSSS aviation weather station's observed daily max —
no other station or data source is used for settlement.

Key concepts:
  winner bracket: the Polymarket bracket whose range contains the actual WSSS
      daily max (e.g. 33.0°C → the "33°C" bracket [32.5, 33.5]).
  lock-on hour: the earliest cycle-hour at which the model's highest-probability
      bracket matches the winner bracket — i.e. the model "called it right."
  held: once the model locks on, does the winner bracket stay on top for all
      subsequent cycles?  If yes the prediction is stable; if not the model
      wobbled and the user should be cautious.
  tradeable lock-on: like lock-on, but also requires the winning bracket's
      model probability to be ≥ MIN_EDGE_CONFIDENCE (≈0.12, loosely corresponding
      to ≥1% expected edge against a fair-priced bracket).

Metrics are computed from the analytics-store snapshots (per-cycle
{ts_sgt, mu, sigma, hour, brackets}) cross-referenced with the prediction
journal's settled actuals.
"""

from collections import defaultdict
from data.analytics_store import get_snapshots
from data.prediction_journal import get_journal

# For a bracket to be "tradeable for ≥1% return" the model needs enough
# confidence that buying YES (at p) would give ≥1% edge.  Without exact
# prices at the snapshot moment, we approximate: if P(win) ≥ this floor the
# model believes there's meaningful edge worth investigating.  This isn't the
# full Kelly edge (which needs price), but captures the "1% target" spirit.
_MIN_EDGE_CONFIDENCE = 0.10


def _bracket_for_temp(temp: float, brackets: list[dict]) -> str | None:
    """Return the bracket title whose range contains `temp`."""
    temp_rounded = round(temp)
    for b in brackets:
        t = b.get("bracket", "").lower()
        if "or below" in t or "or lower" in t:
            # e.g. "28°C or below" → bound = 28+0.5
            val = float(''.join(c for c in t if c.isdigit() or c == '.'))
            if temp <= val + 0.5:
                return b.get("bracket")
        elif "or above" in t or "or higher" in t:
            val = float(''.join(c for c in t if c.isdigit() or c == '.'))
            if temp >= val - 0.5:
                return b.get("bracket")
        else:
            # e.g. "33°C" → range [32.5, 33.5]
            val = float(''.join(c for c in t if c.isdigit() or c == '.'))
            if val - 0.5 <= temp < val + 0.5:
                return b.get("bracket")
    return None


def _group_snapshots_by_date() -> dict[str, list[dict]]:
    """Group analytics snapshots by SGT date, oldest-first within each day."""
    snaps = get_snapshots()
    by_date: dict[str, list] = defaultdict(list)
    for s in snaps:
        date_key = (s.get("ts_sgt") or "")[:10]
        if date_key:
            by_date[date_key].append(s)
    for v in by_date.values():
        v.sort(key=lambda s: s.get("ts_sgt", ""))
    return dict(by_date)


def analyze_early_prediction() -> dict:
    """Per-day lock-on analysis for settled prediction journal days.

    Returns:
      days: per-day breakdown (newest first) with lock-on hour, confidence,
          whether the prediction held, and the end-of-day state.
      summary: aggregate stats (avg lock-on hour, % held, etc.)
    """
    journal = get_journal()
    settled = [e for e in journal if e.get("actual_max") is not None]

    snap_by_date = _group_snapshots_by_date()
    days = []

    for entry in settled:  # newest first from journal
        date_str = entry.get("date_str", "")
        actual_max = entry.get("actual_max")
        if actual_max is None:
            continue
        date_key = date_str.replace("-", "-").replace("/", "-")
        # Normalize to YYYY-MM-DD from the journal's "September-05-2026" form
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
            date_key = f"{yyyy}-{mm}-{dd}"

        snaps = snap_by_date.get(date_key, [])
        if not snaps:
            # No timeseries for this day → report settled result only.
            days.append({
                "date": date_str,
                "date_key": date_key,
                "actual_max": actual_max,
                "winner_bracket": None,
                "lock_on_hour": None,
                "held": None,
                "confidence_at_lock_in": None,
                "end_top_bracket": None,
                "end_top_prob": None,
                "total_snapshots": 0,
                "note": "no timeseries snapshots available",
            })
            continue

        # Determine winner bracket from any bracket reference in the snapshots
        all_brackets = []
        for s in snaps:
            all_brackets.extend(s.get("brackets", []))
        # unique by bracket name
        seen = set()
        unique_brackets = []
        for b in all_brackets:
            if b.get("bracket") not in seen:
                seen.add(b["bracket"])
                unique_brackets.append(b)

        winner_bracket = _bracket_for_temp(actual_max, unique_brackets)
        if winner_bracket is None:
            days.append({
                "date": date_str,
                "date_key": date_key,
                "actual_max": actual_max,
                "winner_bracket": None,
                "lock_on_hour": None,
                "held": None,
                "confidence_at_lock_in": None,
                "end_top_bracket": None,
                "end_top_prob": None,
                "total_snapshots": len(snaps),
                "note": "winner bracket not in snapshot reference set",
            })
            continue

        # Walk snapshots chronologically and find lock-on
        lock_on_hour = None
        confidence_at_lock_in = None
        first_lock_on_occurred = False
        held = True  # until proven otherwise

        for s in snaps:
            brackets = s.get("brackets", [])
            if not brackets:
                continue
            top = max(brackets, key=lambda b: b.get("prob", 0))
            top_bracket = top.get("bracket", "")
            top_prob = top.get("prob", 0)

            if top_bracket == winner_bracket and not first_lock_on_occurred:
                first_lock_on_occurred = True
                lock_on_hour = s.get("hour")
                confidence_at_lock_in = top_prob

            if first_lock_on_occurred and top_bracket != winner_bracket:
                held = False

        # End-of-day state (last snapshot)
        last_brackets = snaps[-1].get("brackets", [])
        end_top = max(last_brackets, key=lambda b: b.get("prob", 0)) if last_brackets else {}

        # Winner confidence at end-of-day
        winner_prob_end = 0.0
        for b in last_brackets:
            if b.get("bracket") == winner_bracket:
                winner_prob_end = b.get("prob", 0)
                break

        days.append({
            "date": date_str,
            "date_key": date_key,
            "actual_max": actual_max,
            "winner_bracket": winner_bracket,
            "lock_on_hour": lock_on_hour,
            "held": held if first_lock_on_occurred else None,
            "confidence_at_lock_in": round(confidence_at_lock_in, 4) if confidence_at_lock_in else None,
            "end_top_bracket": end_top.get("bracket"),
            "end_top_prob": round(end_top.get("prob", 0), 4),
            "winner_prob_end": round(winner_prob_end, 4),
            "total_snapshots": len(snaps),
        })

    # Aggregate summary
    lock_on_hours = [d["lock_on_hour"] for d in days if d["lock_on_hour"] is not None]
    held_count = sum(1 for d in days if d.get("held") is True)
    days_with_lock = len(lock_on_hours)

    # Only count days that actually had snapshot data as "analyzed" —
    # days without timeseries snapshots are settled results, not analysis.
    days_with_data = [d for d in days if d.get("total_snapshots", 0) > 0]

    summary = {
        "days_analyzed": len(days_with_data),
        "days_with_lock_on": days_with_lock,
        "avg_lock_on_hour": round(sum(lock_on_hours) / len(lock_on_hours), 1) if lock_on_hours else None,
        "earliest_lock_on_hour": min(lock_on_hours) if lock_on_hours else None,
        "latest_lock_on_hour": max(lock_on_hours) if lock_on_hours else None,
        "pct_held_after_lock_on": round(held_count / days_with_lock, 2) if days_with_lock else None,
    }

    return {
        "days": days,
        "summary": summary,
        "note": "lock-on = earliest hour the model's top bracket matched the settled winner "
                "and stayed matched; for 1%+ return, the winning bracket must be "
                "purchased below ~$0.99 — i.e. not near-certain.",
    }
