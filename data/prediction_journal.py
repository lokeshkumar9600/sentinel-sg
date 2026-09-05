"""Prediction journal — records each day's (mu, sigma) and stamps the actual
max once the day settles. Stored as a flat JSON file so the performance
dashboard has something to measure against.

Settling logic: once the date_str is in the past and the WSSS METAR for that
day shows a known max temperature, we stamp it.  This is done lazily from
the /api/performance and /api/dashboard endpoints so no background cron is
needed.
"""

import json
import math
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SGT = ZoneInfo("Asia/Singapore")
JOURNAL_FILE = Path("data/predictions.json")

_lock = threading.Lock()


def _load() -> list:
    if JOURNAL_FILE.exists():
        try:
            with open(JOURNAL_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    return []


def _save(entries: list):
    JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL_FILE, "w") as f:
        json.dump(entries, f, indent=2)


def _today_str() -> str:
    # %d (zero-padded, portable) — must round-trip through strptime below.
    return datetime.now(SGT).strftime("%B-%d-%Y")


def record_prediction(mu: float, sigma: float, hour_of_day: int) -> None:
    """Upsert today's prediction.  If already recorded, overwrite mu/sigma/hour
    (the model re-predicts every 30s and we keep the latest)."""
    with _lock:
        entries = _load()
        date_str = _today_str()
        for e in entries:
            if e["date_str"] == date_str:
                e["predicted_mu"] = round(mu, 3)
                e["predicted_sigma"] = round(sigma, 3)
                e["hour_of_day"] = hour_of_day
                e["recorded_at_sgt"] = datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT")
                _save(entries)
                return
        entries.append({
            "date_str": date_str,
            "predicted_mu": round(mu, 3),
            "predicted_sigma": round(sigma, 3),
            "hour_of_day": hour_of_day,
            "actual_max": None,
            "recorded_at_sgt": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT"),
        })
        _save(entries)


def settle_prediction(date_str: str, actual_max: float) -> bool:
    """Stamp the actual max for a settled day.  Returns True if the entry
    was found and updated."""
    with _lock:
        entries = _load()
        for e in entries:
            if e["date_str"] == date_str and e["actual_max"] is None:
                e["actual_max"] = round(actual_max, 3)
                _save(entries)
                return True
        return False


def _try_settle_from_metar(entries: list) -> list:
    """For any unsettled entry whose date is in the past, try to settle it
    from the aviationweather METAR data (fetch is synchronous; callers
    should only invoke this periodically to avoid hammering)."""
    from data.ingestion import fetch_wsss_metar_history  # avoid circular

    today = datetime.now(SGT).date()
    need_fetch = [
        e for e in entries
        if e["actual_max"] is None
        and datetime.strptime(e["date_str"], "%B-%d-%Y").date() < today
    ]
    if not need_fetch:
        return entries

    metars = fetch_wsss_metar_history()
    if not metars:
        return entries

    for e in need_fetch:
        try:
            target_date = datetime.strptime(e["date_str"], "%B-%d-%Y").date()
        except ValueError:
            continue
        day_temps = []
        for m in metars:
            obs_epoch = m.get("obsTime", 0)
            if obs_epoch:
                obs_date = datetime.fromtimestamp(obs_epoch, tz=SGT).date()
                if obs_date == target_date and m.get("temp") is not None:
                    day_temps.append(m["temp"])
        if day_temps:
            e["actual_max"] = round(max(day_temps), 3)

    _save(entries)
    return entries


def get_journal() -> list:
    """Return all journal entries, newest first."""
    with _lock:
        entries = _load()
    return sorted(entries, key=lambda e: e.get("recorded_at_sgt", ""), reverse=True)


def get_performance() -> dict:
    """Compute model performance over all settled predictions.  Returns
    the stats plus the raw journal for the frontend."""
    with _lock:
        entries = _load()
        # Attempt to settle past days from METAR (no more than once per cycle)
        entries = _try_settle_from_metar(entries)

    settled = [e for e in entries if e.get("actual_max") is not None]
    n = len(settled)

    if n == 0:
        return {
            "days_tracked": len(entries),
            "days_settled": 0,
            "mae": None,
            "bias": None,
            "hit_rate_1sigma": None,
            "hit_rate_2sigma": None,
            "bias_direction": "—",
            "journal": entries,
        }

    errors = []
    biases = []
    hits_1 = 0
    hits_2 = 0
    for e in settled:
        mu = e["predicted_mu"]
        sigma = max(0.1, e["predicted_sigma"])
        actual = e["actual_max"]
        err = actual - mu
        errors.append(abs(err))
        biases.append(err)
        if abs(err) <= sigma:
            hits_1 += 1
        if abs(err) <= 2 * sigma:
            hits_2 += 1

    mae = sum(errors) / n
    bias = sum(biases) / n
    direction = "overpredict" if bias > 0 else "underpredict" if bias < 0 else "unbiased"

    return {
        "days_tracked": len(entries),
        "days_settled": n,
        "mae": round(mae, 3),
        "bias": round(bias, 3),
        "hit_rate_1sigma": round(hits_1 / n, 3),
        "hit_rate_2sigma": round(hits_2 / n, 3),
        "bias_direction": direction,
        "journal": entries,
    }
