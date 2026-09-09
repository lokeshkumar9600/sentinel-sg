"""Data-driven climatology for the daily-max prediction.

The prediction previously leaned on a hardcoded `CLIM_MEAN_DEFAULT = 31.3`
(September).  That constant understates reality: the last 9 days of WSSS METAR
history average a September daily max of ~32.8°C.  This module derives the
prior from the ACTUAL observed weather instead of a fixed month constant.

How it works:
  - Reads the cached WSSS METAR history (same source the calibration backtest
    uses, so no extra network dependency on the hot prediction path).
  - Reduces observations to per-day maxima (WSSS is the settlement source).
  - Stores a per-month (or "recent window" when a given month has too few days)
    mean and std of the daily max.
  - Exposes `climatology_for_month(year, month)` returning (mean, std, n_days),
    with graceful fallback to the configured default.

The mean becomes the model's Bayesian prior (replacing `CLIM_MEAN_DEFAULT`);
the std helps set the climatological sigma floor when live signal is weak.

Multi-month shape is learned as data accrues: early on (a handful of days) a
single pooled "recent climatology" covers everything; once a month has enough
days it gets its own statistics; adjacent months are blended when sparse.
"""

import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from data.config import CLIM_MEAN_DEFAULT, CLIM_SIGMA

SGT = ZoneInfo("Asia/Singapore")
CACHE_FILE = Path("data/wsss_metar_cache.json")

# How many settled days a single month must have before it gets its own stats.
MIN_DAYS_PER_MONTH = 5
# Fall back to the pooled "recent window" mean if a month is under-sampled.
RECENT_WINDOW_DAYS = 30
# Guard: reject absurd daily maxima (which would corrupt the climatology).
_MIN_PLAUSIBLE = 24.0
_MAX_PLAUSIBLE = 40.0

_daily_max_cache: dict | None = None  # {(year, month): [daily_max,...]}


def _load_daily_maxes() -> list[tuple]:
    """[(date, daily_max_c)] from the cached METAR history, oldest first."""
    global _daily_max_cache
    if _daily_max_cache is not None:
        return _daily_max_cache

    if not CACHE_FILE.exists():
        _daily_max_cache = []
        return []

    try:
        with open(CACHE_FILE) as f:
            metars = json.load(f)
    except (json.JSONDecodeError, IOError):
        _daily_max_cache = []
        return []

    from collections import defaultdict
    by_day: dict = defaultdict(list)
    for m in metars:
        eps = m.get("obsTime") or 0
        temp = m.get("temp")
        if not eps or temp is None:
            continue
        try:
            day = datetime.fromtimestamp(eps, tz=SGT).date()
        except (OSError, ValueError, OverflowError):
            continue
        t = float(temp)
        if _MIN_PLAUSIBLE <= t <= _MAX_PLAUSIBLE:
            by_day[day].append(t)

    rows = []
    for day in sorted(by_day):
        rows.append((day, max(by_day[day])))
    _daily_max_cache = rows
    return rows


def _month_stats(year: int, month: int, rows: list[tuple]) -> tuple[float, float, int] | None:
    """Mean/std/n of the daily max for one calendar month."""
    vals = [v for (d, v) in rows if d.year == year and d.month == month]
    if not vals:
        return None
    n = len(vals)
    mean = sum(vals) / n
    if n == 1:
        spread = 0.4  # single sample: lean on climatological spread
    else:
        spread = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1))
    return mean, spread, n


def climatology_for_month(year: int = None, month: int = None) -> dict:
    """Return the climatological prior for a (year, month).

    Strategy:
      1. If that calendar month has >= MIN_DAYS_PER_MONTH of WSSS daily maxes,
         return its own mean/std.
      2. Else blend with neighboring months' data (weighted by sample count).
      3. Else use the pooled recent window.
      4. Else fall back to the configured CLIM_MEAN_DEFAULT / CLIM_SIGMA.

    Returns {mean, std, n_days, source}.  `source` describes which level the
    prior came from so the UI can show why changing climatology moved.
    """
    now = datetime.now(SGT)
    year = year or now.year
    month = month or now.month
    rows = _load_daily_maxes()

    if not rows:
        return {
            "mean": CLIM_MEAN_DEFAULT,
            "std": CLIM_SIGMA,
            "n_days": 0,
            "source": "default",
        }

    # 1. This month's own stats.
    own = _month_stats(year, month, rows)
    if own is not None and own[2] >= MIN_DAYS_PER_MONTH:
        return {
            "mean": round(own[0], 2),
            "std": round(max(own[1], 0.3), 2),
            "n_days": own[2],
            "source": f"this-month ({month:02d}/{year})",
        }

    # 2. Blend with neighbors: the previous month and, if data exists, the same
    # month last year (cross-year persistence of Singapore's monsoon seasons).
    import datetime as _dt
    candidate_months = []
    prev_ym = (year - 1, 12) if month == 1 else (year, month - 1)
    same_last_year = (year - 1, month)
    for (yy, mm) in {prev_ym, same_last_year}:
        st = _month_stats(yy, mm, rows)
        if st is not None:
            candidate_months.append(st)
    if own is not None:
        candidate_months.append(own)

    if candidate_months:
        wsum = 0.0
        wmean = 0.0
        wvar_sum = 0.0
        ndays = 0
        for (mean, spread, n) in candidate_months:
            wmean += mean * n
            wvar_sum += (spread ** 2) * n
            wsum += n
            ndays += n
        mean = wmean / wsum
        # Pooled variance across the candidate months.
        var = wvar_sum / wsum
        return {
            "mean": round(mean, 2),
            "std": round(max(math.sqrt(var), 0.3), 2),
            "n_days": ndays,
            "source": "month-blend",
        }

    # 3. Pooled recent window (all daily maxes in the cache).
    recent = [v for (d, v) in rows if (now.date() - d).days <= RECENT_WINDOW_DAYS]
    if len(recent) >= MIN_DAYS_PER_MONTH:
        mean = sum(recent) / len(recent)
        spread = math.sqrt(sum((v - mean) ** 2 for v in recent) / len(recent)) if len(recent) > 1 else 0.4
        return {
            "mean": round(mean, 2),
            "std": round(max(spread, 0.3), 2),
            "n_days": len(recent),
            "source": f"recent-{len(recent)}d",
        }

    # 4. Fallback.
    return {
        "mean": CLIM_MEAN_DEFAULT,
        "std": CLIM_SIGMA,
        "n_days": len(recent),
        "source": "default",
    }


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(climatology_for_month(), indent=2))