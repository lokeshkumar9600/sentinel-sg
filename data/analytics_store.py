"""Analytics-store — rolling time series of model predictions.

The prediction journal keeps ONE (mu, sigma) per settled day; that can't draw a
"predictions over time" chart. This store snapshots every evaluate cycle's
prediction (mu, sigma and each bracket's model probability) with its SGT
timestamp, so the analytics page can plot how the model's bracket probabilities
moved through the day. File-backed so history survives restarts; capped so it
never grows unbounded.
"""

import json
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SGT = ZoneInfo("Asia/Singapore")
STORE_FILE = Path("data/prediction_timeseries.json")

# Keep at most this many snapshots (a cycle runs ~every 30s, so this is ~12h).
MAX_SNAPSHOTS = 2000

_lock = threading.Lock()


def _load() -> list:
    if STORE_FILE.exists():
        try:
            with open(STORE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    return []


def _save(snapshots: list):
    STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STORE_FILE, "w") as f:
        json.dump(snapshots[-MAX_SNAPSHOTS:], f, indent=2)


def record_snapshot(mu: float, sigma: float, bracket_probs: list[dict],
                    hour_of_day: float | None = None) -> None:
    """Append one prediction snapshot: {ts, mu, sigma, hour, brackets}.

    `bracket_probs` is a list of {"bracket": str, "prob": float} — the model's
    live probability for each open bracket at this instant.
    """
    with _lock:
        snapshots = _load()
        snapshots.append({
            "ts_sgt": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT"),
            "mu": round(float(mu), 3),
            "sigma": round(float(sigma), 3),
            "hour": round(hour_of_day) if hour_of_day is not None else None,
            "brackets": bracket_probs,
        })
        _save(snapshots)


def get_snapshots(limit: int | None = None) -> list:
    """Return snapshots oldest-first, up to `limit` (default: all)."""
    with _lock:
        snaps = _load()
    if limit:
        snaps = snaps[-limit:]
    return snaps


def clear() -> None:
    with _lock:
        _save([])