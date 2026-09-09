"""FastAPI backend for the SG Max-Temp dashboard.

Exposes the CLI-only pipeline from main.py over HTTP and serves the static
single-page dashboard (static/) from the same origin - so the frontend just
fetches relative /api/... URLs with no CORS setup.

Endpoints:
    GET /                -> static dashboard (index.html)
    GET /api/health      -> lightweight, no network
    GET /api/dashboard   -> full pipeline: features + prediction + live markets
    GET /api/prices      -> live bracket prices from the WebSocket feed (instant)

The WebSocket feed (execution/ws_feed.py) runs as a background daemon thread
started on app startup.  /api/prices reads from its in-memory price map with
zero latency — no TTL cache, no Gamma polling.
"""

import logging
import math
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import numpy as np


def _f(v, default: float):
    try:
        f = float(v)
        return f if f == f else default  # NaN check
    except (TypeError, ValueError):
        return default

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("sentinel")

from data.config import (
    BANKROLL_USD,
    DAILY_LOSS_LIMIT_PCT,
    ENTRY_WINDOW_HOURS,
    TRADEABLE_HOURS_END,
    TRADEABLE_HOURS_START,
)
from data.feature_engine import extract_singapore_feature_vector
from data.ingestion import fetch_all_data_gov, fetch_wsss_metar_history
from data.analytics_store import get_snapshots, record_snapshot
from data.prediction_journal import get_performance, record_prediction
from data.model_learner import self_tune
from data.spatial import extract_spatial_layers
from execution.positions import (  # noqa: F401  (RESOLVED_OR_STALE used in helpers)
    RESOLVED_OR_STALE,
    PositionBook,
)
from execution.trade_history import log_entry, log_exit, log_signal_only, get_history
from execution.ws_feed import LiveFeed
from main import (
    POLL_INTERVAL_SECONDS,
    SGT,
    _diurnal_heating_fraction,
    _convection_storm_score,
    _build_prediction_context,
    evaluate_polymarket_brackets,
    find_live_event,
    predict_daily_max_temp,
)
from data.storm_timing import compute_storm_timing_factor

# ---------------------------------------------------------------------------
# Global feed + advisory position book — started/stopped by lifespan below.
# ---------------------------------------------------------------------------
feed = LiveFeed()
book = PositionBook()

# Latest model state (mu/sigma/diurnal-heating) so the advisory book can run its
# model-driven fair-value guard rail on every price tick without re-running the
# full pipeline. Updated by _get_dashboard and simulate whenever they predict.
_last_model = {"mu": None, "sigma": None, "hour_of_day": None, "at": 0.0}

# Daily realized-loss tracker (circuit breaker). Reset at the first refresh of a
# new SGT day; entries are gated off once realized losses for the day breach
# DAILY_LOSS_LIMIT_PCT of bankroll.
_day_loss = {"date": None, "usd": 0.0}


def _cache_model(mu: float, sigma: float, hour_of_day: float) -> None:
    _last_model["mu"] = mu
    _last_model["sigma"] = sigma
    _last_model["hour_of_day"] = hour_of_day
    _last_model["at"] = time.time()


def _today_sgt() -> str:
    return datetime.now(SGT).strftime("%Y-%m-%d")


def _accumulate_day_loss(closed_pos: dict) -> None:
    """Add a settled position's realized P&L (USD) to the day's running total,
    resetting the counter when the SGT date rolls over."""
    today = _today_sgt()
    if _day_loss["date"] != today:
        _day_loss["date"] = today
        _day_loss["usd"] = 0.0
    stake = closed_pos.get("stake_usd") or 0.0
    pnl_pct = closed_pos.get("pnl_pct") or 0.0
    # For a position bought at entry and sold at exit, realized USD = stake*pnl_pct.
    _day_loss["usd"] += stake * pnl_pct


def _daily_loss_hit() -> bool:
    """True once today's realized losses exceed DAILY_LOSS_LIMIT_PCT of bankroll."""
    if _day_loss["date"] != _today_sgt():
        _day_loss["date"] = _today_sgt()
        _day_loss["usd"] = 0.0
        return False
    if not BANKROLL_USD:
        return False
    return _day_loss["usd"] <= -BANKROLL_USD * DAILY_LOSS_LIMIT_PCT


def _sell_map(snapshot: list[dict]) -> dict:
    """Map feed snapshot brackets to their exit (sell) prices, keyed by
    "bracket|side". These are what a position could close at right now: for a YES
    position that's best bid, for a NO position it's the negRisk NO bid."""
    m = {}
    for b in snapshot:
        title = b.get("bracket")
        if not title:
            continue
        if b.get("yes_sell") is not None:
            m[f"{title}|YES"] = b["yes_sell"]
        if b.get("no_sell") is not None:
            m[f"{title}|NO"] = b["no_sell"]
    return m


def _refresh_book_from_feed(snapshot: list[dict]):
    """Feed live sell prices into the book, mark day-rolled brackets resolved,
    and settle any position whose exit action has triggered (advisory auto-close)."""
    if not snapshot:
        return
    live_titles = {b.get("bracket") for b in snapshot if b.get("bracket")}
    # Any position whose bracket is no longer in the live feed (event resolved /
    # rolled forward) is settled out as resolved.
    stale_keys = [
        k for k in book.snapshot()
        if k["bracket"] not in live_titles
    ]
    if stale_keys:
        book.mark_resolved([f"{p['bracket']}|{p['side']}" for p in stale_keys])
    book.update_prices(_sell_map(snapshot))
    # Model-driven guard rails: fair-value exit + lockout harvest. Uses the most
    # recent cached mu/sigma to compute each held bracket's model fair value, so a
    # collapsing position is exited on the model's re-rating BEFORE the market
    # fully reprices (this is what catches the -90% gap-down stops).
    _run_guard_rails(snapshot)
    # Log exits when positions settle (take-profit / stop / resolved) and fold
    # their realized P&L into the daily-loss circuit breaker.
    closed = book.settle_actions()
    for c in closed:
        log_exit(c["bracket"], c["side"], c["exit_price"], c["pnl_pct"],
                 c.get("reason") or c["closed_action"],
                 entry_price=c.get("entry_price"))
        _accumulate_day_loss(c)


def _run_guard_rails(snapshot: list[dict]) -> None:
    """Compute model fair values for held positions and run the guard rails.

    Fair value for a bracket's YES side is the model's live probability of that
    bracket; NO fair = 1 - prob. Only runs if we have a fresh-enough cached
    prediction (same-day), so the model-driven exit never acts on stale mu/sigma.
    """
    mu = _last_model.get("mu")
    sigma = _last_model.get("sigma")
    hour = _last_model.get("hour_of_day")
    at = _last_model.get("at", 0.0)
    if mu is None or sigma is None:
        return
    # Don't act on a model older than ~15 minutes (covers dashboard TTL gaps).
    if time.time() - at > 900:
        return
    try:
        from execution.kelly_sizer import calculate_bracket_probability
        from execution.polymarket import parse_temperature_bounds
        fair = {}
        for pos in book.snapshot():
            low, high = parse_temperature_bounds(pos["bracket"])
            if low is None or high is None:
                continue
            prob = calculate_bracket_probability(low, high, mu, sigma)
            # fair[bracket|YES] = P(temp in bracket), fair[bracket|NO] = 1 - P(temp in bracket)
            if pos["side"] == "NO":
                fair[f"{pos['bracket']}|{pos['side']}"] = 1.0 - prob
            else:
                fair[f"{pos['bracket']}|{pos['side']}"] = prob
        df = _diurnal_heating_fraction(hour) if hour is not None else None
        book.manage_guard_rails(fair, df)
    except Exception:  # noqa: BLE001 — guard rails must never break the tick loop
        pass


def _run_entry_gate(trades: list[dict], hour_of_day: float, snapshot: list[dict], features: dict) -> None:
    """When-to-trade layer: promote a pipeline BUY_YES/BUY_NO into an actual
    (advisory) entry only when timing + risk conditions hold, and register it in
    the book so it can be managed to take-profit/stop afterwards.

    Entry conditions:
      - Not already holding that bracket+side (no doubling down).
      - A live exit (sell) price exists — if we can't manage the risk later we
        don't take the entry.
      - Timing gate: hour is inside the golden window (mu formed, market not yet
        repriced), OR the day is near-final (diurnal heating ~1) but a durable
        edge still remains — the "lockout" case where the running max is settled.
    """
    if not trades:
        return
    df = _diurnal_heating_fraction(hour_of_day)
    lo, hi = ENTRY_WINDOW_HOURS
    sell = _sell_map(snapshot)
    # Daily-loss circuit breaker: after realized losses for the SGT day breach
    # DAILY_LOSS_LIMIT_PCT of bankroll, stop staging any new entries until tomorrow.
    if _daily_loss_hit():
        for t in trades:
            if t.get("action") in ("BUY_YES", "BUY_NO"):
                t["action"] = "DAILY_STOP"
                t["reason"] = "Daily loss limit reached — entries halted until tomorrow"
                log_signal_only("DAILY_STOP", t.get("bracket", ""), t.get("edge", 0),
                                "Daily loss limit reached")
        return
    for t in trades:
        action = t.get("action")
        if action == "BUY_YES":
            side = "YES"
        elif action == "BUY_NO":
            side = "NO"
        else:
            # Log non-entry signals for history
            reason = t.get("reason", "")
            log_signal_only(action or "NO_TRADE", t.get("bracket", ""), t.get("edge", 0), reason)
            continue  # SKIP / NO_TRADE already ruled out by the edge threshold

        bracket = t.get("bracket")
        if not bracket:
            continue
        key = f"{bracket}|{side}"

        if book.has(bracket, side):
            # Already holding — the live manage state (HOLD/TAKE_PROFIT/STOP)
            # surfaces separately via /api/positions; don't re-enter.
            t["action"] = f"HOLD_{side}"
            t["reason"] = "Position already open"
            log_signal_only(f"HOLD_{side}", bracket, t.get("edge", 0), "Position already open")
            continue

        exit_price = sell.get(key)
        if exit_price in (None, 0.0):
            t["action"] = "SKIP"
            t["reason"] = "No exit liquidity to manage risk"
            log_signal_only("SKIP", bracket, t.get("edge", 0), "No exit liquidity to manage risk")
            continue

        in_window = lo <= hour_of_day <= hi
        near_final = df >= 0.97
        if not (in_window or near_final):
            t["action"] = "TIMING_HOLD"
            t["reason"] = f"Outside entry window ({lo}:00-{hi}:00 SGT)"
            log_signal_only("TIMING_HOLD", bracket, t.get("edge", 0), f"Outside window ({lo}:00-{hi}:00)")
            continue

        entry_price = t.get("yes_price") if side == "YES" else t.get("no_price")
        if not entry_price or entry_price >= 1.0:
            t["action"] = "SKIP"
            t["reason"] = "Bad entry price (no usable ask)"
            log_signal_only("SKIP", bracket, t.get("edge", 0), "Bad entry price")
            continue

        entered = book.enter(
            bracket, side, entry_price,
            t.get("stake_usd", 0.0),
            t.get("prob"), t.get("edge"),
            hour_of_day,
        )
        if entered:
            t["action"] = f"ENTER_{side}"
            log_entry(bracket, side, entry_price, t.get("stake_usd", 0.0), t.get("edge", 0))


def _build_signal_state(features: dict, trades: list[dict]) -> dict:
    """Summarise the decision status for the UI: is today's model signal live or
    still an overnight prior? Any bracket priced off the WebSocket feed? Shares
    its window predicate with _run_entry_gate so the banner can never disagree
    with the gate that turns signals into entries."""
    hour = _f(features.get("hour_of_day"), 12.0)
    df = _diurnal_heating_fraction(hour)
    live_window = TRADEABLE_HOURS_START <= hour <= TRADEABLE_HOURS_END
    tradeable = live_window or df >= 0.97
    live_priced = any(t.get("priced_from_live") for t in (trades or []))

    if not tradeable:
        status = "no_signal"
        label = f"Overnight — no formed signal yet ({int(hour):02d}:00 SGT)"
    elif live_priced:
        status = "live"
        label = "Live signal — prices from WebSocket feed"
    else:
        status = "formed"
        label = f"Signal formed {int(hour):02d}:00 SGT — prices from market metadata"
    return {
        "status": status,
        "label": label,
        "tradeable": bool(tradeable),
        "live_priced": live_priced,
        "hour_of_day": hour,
        "df": round(df, 3),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown."""
    # Startup
    logger.info("Starting Sentinel Prediction Engine...")
    logger.info("Initializing WebSocket feed...")
    feed.start()
    logger.info("Server ready. Dashboard available at http://localhost:8000")
    yield
    # Shutdown
    logger.info("Shutting down Sentinel Prediction Engine...")
    feed.stop()
    logger.info("WebSocket feed stopped.")
    logger.info("Shutdown complete.")


app = FastAPI(title="SG Max-Temp Dashboard", version="1.0.0", lifespan=lifespan)


# --- Request logging middleware ---
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log all API requests with timing information."""
    start_time = time.time()
    response = await call_next(request)
    duration = time.time() - start_time

    # Only log API endpoints, not static files
    if request.url.path.startswith("/api/"):
        logger.info(
            "%s %s %d %.3fms",
            request.method,
            request.url.path,
            response.status_code,
            duration * 1000,
        )

    return response


# ---------------------------------------------------------------------------
# Simple in-memory rate limiting for API endpoints.
#
# Token-bucket per client IP (proxy-aware: honours X-Forwarded-For). Heavy
# endpoints (/api/dashboard) get a tighter budget than cheap ones. /api/prices
# is polled ~10x/sec by the dashboard, so it and /api/health are exempted.
# Falls back to permissive when the classifier hit is absent. Pure in-memory —
# resets on process restart, which is fine for a single-instance dashboard.
# ---------------------------------------------------------------------------
from collections import defaultdict
import os as _os

RATE_LIMIT_DEFAULT = int(_os.getenv("RATE_LIMIT_DEFAULT_RPS", "5"))
RATE_LIMIT_DASHBOARD = int(_os.getenv("RATE_LIMIT_DASHBOARD_RPM", "60"))

# Endpoints exempt from rate limiting (polled constantly by the frontend).
_RATE_LIMIT_EXEMPT = frozenset({"/api/prices", "/api/health"})
# Endpoints with extra-strict budgets.
_RATE_LIMIT_STRICT = frozenset({"/api/dashboard"})

_buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_refill)
_bucket_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit_key(path: str, client_ip: str) -> str:
    if path in _RATE_LIMIT_STRICT:
        return f"strict:{client_ip}"
    return f"default:{client_ip}"


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or path in _RATE_LIMIT_EXEMPT:
        return await call_next(request)

    client_ip = _client_ip(request)
    # Rate constants: tokens is a float "burst allowance"; it refills at RPS.
    if path in _RATE_LIMIT_STRICT:
        rps = RATE_LIMIT_DASHBOARD / 60.0
        burst = RATE_LIMIT_DASHBOARD
    else:
        rps = float(RATE_LIMIT_DEFAULT)
        burst = RATE_LIMIT_DEFAULT * 10

    key = _rate_limit_key(path, client_ip)
    now = time.monotonic()
    with _bucket_lock:
        tokens, last = _buckets.get(key, (burst, now))
        refill = (now - last) * rps
        tokens = min(burst, tokens + refill)
        _buckets[key] = (tokens, now)

        if tokens < 1.0:
            # Prune idle keys occasionally so the map doesn't grow unbounded.
            if len(_buckets) > 1000:
                cutoff = now - 300
                stale = [k for k, (_, t) in _buckets.items() if t < cutoff]
                for k in stale:
                    del _buckets[k]
            return JSONResponse(
                {"detail": "Rate limit exceeded — please slow down."},
                status_code=429,
            )
        _buckets[key] = (tokens - 1.0, now)

    return await call_next(request)

# ---------------------------------------------------------------------------
# TTL cache for /api/dashboard only (full pipeline).  /api/prices is now
# served directly from the in-memory WS feed — no cache needed.
# ---------------------------------------------------------------------------
DASHBOARD_TTL_SECONDS = 10
_dashboard_cache: dict = {"at": 0.0, "payload": None}


def _get_dashboard(fresh: bool = False) -> dict:
    now = time.time()
    if not fresh and _dashboard_cache["payload"] is not None and (now - _dashboard_cache["at"]) < DASHBOARD_TTL_SECONDS:
        return _dashboard_cache["payload"]

    generated_at_sgt = datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT")

    # 1-2. Ingest + build the feature vector.
    raw_gov = fetch_all_data_gov()
    wsss = fetch_wsss_metar_history()
    features = extract_singapore_feature_vector(raw_gov, wsss)

    # 3. Predict today's max-temp distribution.
    mu, sigma = predict_daily_max_temp(features)
    storm = _convection_storm_score(features)
    timing = compute_storm_timing_factor(features)
    context = _build_prediction_context(features, storm, mu, sigma, timing)
    _cache_model(mu, sigma, features.get("hour_of_day", 12))

    # Keep the prediction journal in sync (upserted each cycle today).
    try:
        record_prediction(mu, sigma, _f(features.get("hour_of_day"), 12.0))
    except Exception:  # noqa: BLE001 — journaling must never fail the dashboard
        pass

    # 4-5. Find the live Polymarket event and price the brackets.
    event = None
    snapshot = feed.snapshot()
    hour_of_day = features.get("hour_of_day", 12)
    try:
        event_date_str, markets = find_live_event()
        if markets:
            result = evaluate_polymarket_brackets(
                event_date_str, mu, sigma, markets,
                todays_max_so_far=features.get("wsss_todays_max_so_far"),
                hour_of_day=hour_of_day,
                live_prices=snapshot,
            )
            trades = result["trades"]
            # When-to-trade: turn a promising bracket into an advisory book entry
            # (and fold any already-open position's manage signals in).
            _run_entry_gate(trades, hour_of_day, snapshot, features)
            event = {
                "date_str": result["event_date_str"],
                "trades": trades,
            }
            # Analytics: snapshot this cycle's bracket probabilities (with their
            # timestamp) so the analytics page can plot predictions over time.
            # Must not leak into the event dict — record_snapshot is fire-and-forget.
            try:
                record_snapshot(
                    mu, sigma,
                    [{"bracket": t.get("bracket"), "prob": t.get("prob")} for t in trades if t.get("bracket")],
                    hour_of_day,
                )
            except Exception:  # noqa: BLE001 — analytics must never destroy trades
                pass
        else:
            event = {"date_str": event_date_str, "error": "No open event found within the lookahead window."}
    except Exception as e:  # noqa: BLE001
        event = {"date_str": None, "error": str(e)}

    signal_state = _build_signal_state(features, trades if isinstance(event, dict) and "trades" in event else [])

    payload = {
        "generated_at_sgt": generated_at_sgt,
        "prediction": {
            "mean_c": round(mu, 2),
            "std_c": round(sigma, 2),
            "hour_of_day": hour_of_day,
            "signal_state": signal_state,
            "tuning": self_tune(),
        },
        "context": context,
        "features": features,
        "event": event,
        "positions": book.snapshot(),
    }

    _dashboard_cache["at"] = now
    _dashboard_cache["payload"] = payload
    return payload


@app.get("/api/health")
def health():
    """Enhanced health check with detailed status information."""
    now = datetime.now(SGT)
    return {
        "status": "ok",
        "server_time_sgt": now.strftime("%Y-%m-%d %H:%M:%S SGT"),
        "version": "1.0.0",
        "poll_interval_s": POLL_INTERVAL_SECONDS,
        "dashboard_ttl_s": DASHBOARD_TTL_SECONDS,
        "ws_feed": {
            "connected": feed.connected,
            "tick_count": feed.tick_count,
            "last_move_at": (
                datetime.fromtimestamp(feed.last_move_at, SGT).strftime("%H:%M:%S.%f")[:-3]
                if feed.last_move_at else None
            ),
            "event_date_str": feed.event_date_str,
        },
        "positions": {
            "count": len(book),
        },
        "model": {
            "last_prediction": _last_model.get("at"),
            "age_seconds": round(time.time() - _last_model.get("at", 0)) if _last_model.get("at") else None,
        },
    }


@app.get("/api/dashboard")
def dashboard(fresh: bool = False):
    return JSONResponse(content=_get_dashboard(fresh=fresh))


@app.get("/api/prices")
def prices():
    """Live bracket prices from the WebSocket feed.

    Reads directly from the in-memory price map maintained by the background
    WS daemon — zero network calls, zero TTL cache.  The frontend polls this
    at 0.1s and gets instant price ticks the moment Polymarket quotes move.
    """
    event_date_str = feed.event_date_str
    if not event_date_str:
        return JSONResponse(content={
            "error": "No live event (feed not connected)",
            "generated_at_sgt": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT"),
        })

    snapshot = feed.snapshot()
    # Keep the advisory book's P&L / exit actions in lockstep with live quotes.
    _refresh_book_from_feed(snapshot)
    return JSONResponse(content={
        "event_date_str": event_date_str,
        "generated_at_sgt": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT"),
        "brackets": snapshot,
        "positions": book.snapshot(),
        "feed": {
            "connected": feed.connected,
            "tick_count": feed.tick_count,
            "last_move_at": (
                datetime.fromtimestamp(feed.last_move_at, SGT).strftime("%H:%M:%S.%f")[:-3]
                if feed.last_move_at else None
            ),
        },
    })


@app.get("/api/positions")
def positions():
    """Live advisory position book — what the model has entered and when to exit,
    mapped against current sell prices. P&L and per-position action (HOLD /
    TAKE_PROFIT / STOP / RESOLVED_OR_STALE) are recomputed on each price tick."""
    if not feed.event_date_str:
        return JSONResponse(content={"error": "No live event (feed not connected)", "positions": []})
    _refresh_book_from_feed(feed.snapshot())
    return JSONResponse(content={
        "event_date_str": feed.event_date_str,
        "generated_at_sgt": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT"),
        "positions": book.snapshot(),
    })


@app.get("/api/history")
def history(limit: int = 50):
    """Return the recent signal and trade history."""
    return JSONResponse(content={
        "generated_at_sgt": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT"),
        "history": get_history(limit),
    })


@app.get("/api/spatial")
def spatial():
    """Live spatial layers from all data.gov.sg APIs — station-level readings
    for the map (temp, rain, humidity, wind, lightning), plus region UV/WBGT,
    forecasts, and radar URL. Cached ~8s to avoid hammering the APIs."""
    now = time.time()
    if not hasattr(spatial, "_cache"):
        spatial._cache = {"at": 0.0, "payload": None}
    if spatial._cache["payload"] is not None and (now - spatial._cache["at"]) < 8.0:
        return JSONResponse(content=spatial._cache["payload"])

    raw_gov = fetch_all_data_gov()
    payload = extract_spatial_layers(raw_gov)
    spatial._cache["at"] = now
    spatial._cache["payload"] = payload
    return JSONResponse(content=payload)


@app.get("/api/wsss")
def wsss():
    """Live WSSS METAR — latest report with all display fields + 24h history
    for the sparkline."""
    metar_history = fetch_wsss_metar_history()
    if not metar_history:
        return JSONResponse(content={
            "error": "No METAR data available",
            "generated_at_sgt": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT"),
            "latest": None,
            "history": [],
        })

    # Sort by obsTime descending
    sorted_history = sorted(metar_history, key=lambda m: m.get("obsTime", 0), reverse=True)
    latest = sorted_history[0] if sorted_history else {}

    # Build 24h temp history series (downsampled to ~5 min intervals for bandwidth)
    history = []
    seen_minutes = set()
    for m in sorted_history:
        if "obsTime" not in m or m.get("temp") is None:
            continue
        minute = int(m["obsTime"] // 300)  # 5-min buckets
        if minute in seen_minutes:
            continue
        seen_minutes.add(minute)
        history.append({"t": m["obsTime"], "temp": m["temp"]})
    history.reverse()  # chronological for sparkline

    # Compute RH from temp/dewp
    temp = _f(latest.get("temp"), 28.0)
    dewp = _f(latest.get("dewp"), 24.0)
    rh = max(0.0, min(100.0, 100.0 * math.exp(
        (17.625 * dewp) / (243.04 + dewp) - (17.625 * temp) / (243.04 + temp)
    )))

    # Flight category from visibility + ceiling
    visib = _f(str(latest.get("visib", "")).replace("+", ""), 10.0)
    clouds = latest.get("clouds", [])
    ceiling = 0
    if isinstance(clouds, list):
        for layer in clouds:
            cover = str(layer.get("cover", "FEW")).upper()
            if cover in ("BKN", "OVC", "OVX"):
                base = _f(layer.get("base"), 0.0)
                if base > 0 and (ceiling == 0 or base < ceiling):
                    ceiling = base
    if visib >= 5 and ceiling >= 3000:
        flight_cat = "VFR"
    elif visib >= 3 and ceiling >= 1000:
        flight_cat = "MVFR"
    elif visib >= 1 and ceiling >= 500:
        flight_cat = "IFR"
    else:
        flight_cat = "LIFR"

    # Pressure trend 3h
    press_trend_3h = 0.0
    now_epoch = _f(latest.get("obsTime"), 0.0)
    ref = now_epoch - 3 * 3600
    older = next((m for m in sorted_history if _f(m.get("obsTime"), 0.0) <= ref), None)
    if older:
        press_trend_3h = round(_f(latest.get("altim"), 1013.0) - _f(older.get("altim"), 1013.0), 1)

    # Wind gust
    gust = _f(latest.get("gust"), _f(latest.get("wspd"), 5.0))

    payload = {
        "generated_at_sgt": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT"),
        "latest": {
            "temp": temp,
            "dewp": dewp,
            "rh": round(rh, 1),
            "wspd": _f(latest.get("wspd"), 5.0),
            "wdir": _f(latest.get("wdir"), 0.0),
            "gust": gust,
            "altim": _f(latest.get("altim"), 1013.0),
            "press_trend_3h": press_trend_3h,
            "visib": visib,
            "cloud_oktas": latest.get("clouds", []),
            "low_cloud_ft": 0,  # filled below
            "wxString": latest.get("wxString", ""),
            "flight_category": flight_cat,
            "raw_text": latest.get("rawText", "") or latest.get("rawOb", ""),
            "obs_time_sgt": datetime.fromtimestamp(latest.get("obsTime", 0), tz=SGT).strftime("%H:%M:%S SGT")
                if latest.get("obsTime") else "",
        },
        "history": history,
    }

    # Compute low cloud ft
    total_cloud, lowest_base = 0.0, 0.0
    for layer in latest.get("clouds", []):
        cover = str(layer.get("cover", "FEW")).upper()
        okta = {"CLR":0,"SKC":0,"FEW":2,"SCT":4,"BKN":7,"OVC":8,"OVX":8}.get(cover, 2)
        total_cloud += okta
        base = _f(layer.get("base"), 0.0)
        if base > 0 and (lowest_base == 0 or base < lowest_base):
            lowest_base = base
    payload["latest"]["cloud_oktas"] = min(8.0, total_cloud)
    payload["latest"]["low_cloud_ft"] = lowest_base

    return JSONResponse(content=payload)


@app.get("/api/performance")
def performance():
    """Model accuracy over the prediction journal — MAE, bias, ±1σ/±2σ hit
    rates.  Past unsettled days are lazily settled from the WSSS METAR history
    on each call."""
    return JSONResponse(content=get_performance())


@app.get("/api/early_prediction")
def early_prediction():
    """Per-day analysis of how soon the model calls the right bracket.

    Instead of end-of-day accuracy, this reports the hour at which the model
    first locked onto the settled winner bracket and whether it held — directly
    answering the practical trading question: how early is the signal good
    enough for ≥1% returns?"""
    from data.early_prediction import analyze_early_prediction
    return JSONResponse(content=analyze_early_prediction())


@app.get("/api/backtest")
def backtest():
    """Reconstruct the live advisory book's executed trades from the trade
    history (real fills + stops/take-profits) — see data/backtest.py. Returns an
    equity curve, per-trade rows, and summary stats for the backtest page."""
    from data.backtest import run_backtest  # local import, cheap + keeps startup lean
    return JSONResponse(content=run_backtest())


@app.get("/api/analytics")
def analytics():
    """Time-series data for the analytics page.

    1. metar  — every WSSS METAR temperature observation in the last 36h
                (obsTime -> °C), so the page can plot every reading point.
    2. brackets — the model's prediction history: each evaluate cycle's mu/sigma
                and per-bracket model probabilities, timestamped, so the page can
                plot how bracket probabilities moved through the day.
    """
    metars = fetch_wsss_metar_history()

    from datetime import datetime as _dt
    now = _dt.now(SGT).timestamp()
    seen: dict = {}
    metar_series = []
    for m in metars:
        obs_ts = m.get("obsTime") or m.get("observed") or 0
        t = m.get("temp")
        if not obs_ts or t is None or now - float(obs_ts) > 36 * 3600:
            continue
        # Dedupe overlapping observations: keep the latest reading per second.
        if float(obs_ts) in seen:
            continue
        seen[float(obs_ts)] = True
        try:
            ts_sgt = _dt.fromtimestamp(float(obs_ts), tz=SGT).strftime("%Y-%m-%d %H:%M:%S SGT")
        except (OSError, ValueError, OverflowError):
            continue
        metar_series.append({"ts_sgt": ts_sgt, "temp_c": float(t)})
    metar_series.sort(key=lambda p: p["ts_sgt"])

    snapshots = get_snapshots()

    # ---- throughput & latency stats for the analytics page ----
    cadence_s = 0.0
    if len(metar_series) >= 2:
        try:
            seq_ts = [_dt.strptime(p["ts_sgt"], "%Y-%m-%d %H:%M:%S SGT").timestamp()
                      for p in metar_series]
            gaps = [b - a for a, b in zip(seq_ts, seq_ts[1:]) if b - a > 0]
            if gaps:
                cadence_s = sum(gaps) / len(gaps)
        except (ValueError, OSError):
            cadence_s = 0.0

    stats = _live_feed_stats()
    stats["metar_points"] = len(metar_series)
    stats["metar_cadence_s"] = round(cadence_s) if cadence_s else None
    stats["window_s"] = 36 * 3600

    return JSONResponse(content={
        "generated_at_sgt": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT"),
        "metar": metar_series,
        "prediction_series": snapshots,
        "feed_stats": stats,
    })


def _live_feed_stats() -> dict:
    """Cheap, callable-every-few-seconds feed stats (no METAR network fetch)."""
    import time as _t
    up = feed.uptime()
    last_age = feed.last_tick_age()
    return {
        "connected": feed.connected,
        "ticks_total": feed.tick_count,
        "tick_rate_10s": round(feed.tick_rate(10.0), 2),
        "tick_rate_60s": round(feed.tick_rate(60.0), 2),
        "avg_rate_since_start": round((feed.tick_count / up) if up else 0.0, 2),
        "last_tick_age_ms": None if last_age is None else round(last_age * 1000),
        "uptime_sec": None if up is None else round(up),
    }


@app.get("/api/feed_stats")
def feed_stats():
    """Throughput & latency for the live WebSocket price feed — polled by the
    analytics page's latency panel every few seconds. No METAR dependency, so
    it stays instant even when the observation API is slow."""
    return JSONResponse(content={"feed_stats": _live_feed_stats()})


@app.get("/api/export")
def export_data(
    format: str = "json",
    include_predictions: bool = True,
    include_trades: bool = True,
    include_positions: bool = True,
    limit: int = 1000,
):
    """Export historical data for external analysis.

    Supports JSON format with configurable data inclusion.
    Useful for spreadsheet analysis, ML training, or archival.
    """
    from datetime import datetime as _dt

    export = {
        "generated_at_sgt": _dt.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT"),
        "format": format,
    }

    if include_predictions:
        try:
            from data.prediction_journal import get_journal
            journal = get_journal()
            export["predictions"] = journal[:limit] if journal else []
        except Exception as e:
            logger.warning("Failed to export predictions: %s", e)
            export["predictions"] = []

    if include_trades:
        try:
            from execution.trade_history import get_history
            export["trades"] = get_history(limit)
        except Exception as e:
            logger.warning("Failed to export trades: %s", e)
            export["trades"] = []

    if include_positions:
        try:
            export["positions"] = book.snapshot()
        except Exception as e:
            logger.warning("Failed to export positions: %s", e)
            export["positions"] = []

    return JSONResponse(content=export)


@app.get("/api/export/csv")
def export_csv(
    data_type: str = "trades",
    limit: int = 1000,
):
    """Export data in CSV format for spreadsheet analysis.

    Supported data_types: trades, predictions
    """
    from datetime import datetime as _dt
    import csv
    import io

    if data_type == "trades":
        try:
            from execution.trade_history import get_history
            records = get_history(limit)
            if not records:
                return JSONResponse(content={"error": "No trade data available"}, status_code=404)

            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=records[0].keys())
            writer.writeheader()
            writer.writerows(records)

            return JSONResponse(content={
                "format": "csv",
                "data_type": data_type,
                "count": len(records),
                "csv": output.getvalue(),
            })
        except Exception as e:
            return JSONResponse(content={"error": str(e)}, status_code=500)

    elif data_type == "predictions":
        try:
            from data.prediction_journal import get_journal
            records = get_journal()
            if not records:
                return JSONResponse(content={"error": "No prediction data available"}, status_code=404)

            records = records[:limit]
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=records[0].keys())
            writer.writeheader()
            writer.writerows(records)

            return JSONResponse(content={
                "format": "csv",
                "data_type": data_type,
                "count": len(records),
                "csv": output.getvalue(),
            })
        except Exception as e:
            return JSONResponse(content={"error": str(e)}, status_code=500)

    else:
        return JSONResponse(
            content={"error": f"Unsupported data_type: {data_type}. Use 'trades' or 'predictions'."},
            status_code=400,
        )


def _build_synthetic_features(p: dict) -> dict:
    """Turn a scenario-builder payload into the feature dict the model reads, so
    the simulate endpoint exercises the exact same code path as the live
    dashboard."""
    hour = _f(p.get("hour"), 10.0)
    temp = _f(p.get("temp"), 30.5)
    rh = _f(p.get("rh"), 70.0)
    wind = _f(p.get("wind"), 5.0)
    dewp = _f(p.get("dewp"), 27.0)
    cloud = _f(p.get("cloud"), 4.0)
    storm = bool(p.get("storm", False))

    # Diurnal-ish values so the synthetic day behaves like a real one.
    uv = max(0.3, min(11.0, (1.0 + 0.9 * (10.0 - abs(hour - 13.0))) * (1.0 - cloud * 0.08)))
    ramp = max(-3.0, min(3.0, 3.0 - (hour - 12.0)))

    return {
        "wsss_todays_max_so_far": temp - 0.5,
        "wsss_current_temp": temp,
        "wsss_dewp": dewp,
        "wsss_wspd": wind,
        "wsss_dpd": temp - dewp,
        "wsss_rh": rh,
        "wsss_altim": 1009.0,
        "wsss_visib_num": 10.0,
        "wsss_storm_txt": "TS" if storm else "",
        "wsss_total_cloud_oktas": cloud,
        "wsss_low_cloud_ft": 2200 if storm else 3000 + cloud * 300,
        "wsss_press_trend_3h": -1.2 if storm else -0.2,
        "wsss_temp_ramp_3h": ramp,
        "minutes_since_last_metar": 5.0,
        "uv_index": uv,
        "wbgt_max": 0.5 * temp + 0.2 * dewp + 4.0,
        "lightning_strike_count": 6 if storm else 0,
        "hour_of_day": hour,
        "rain_station_ratio": 0.4 if storm else 0.03,
        "rain_hotspot_ratio": 0.6 if storm else 0.02,
        "rain_dist_to_changi_km": 3.5 if storm else 18.0,
        "changi_forecast_storm": storm,
        "spatial_max_temp": temp + 0.2,
        "spatial_temp_spread": 3.0 if storm else 1.6,
    }


@app.post("/api/simulate")
def simulate(payload: dict = Body(...)):
    """Run the full model against a synthetic scenario: predict, pull the live
    bracket markets, evaluate, then Monte-Carlo the outcome distribution for
    win-rate / expected P&L per bracket.  Purely advisory — never orders."""
    features = _build_synthetic_features(payload)

    mu, sigma = predict_daily_max_temp(features)
    storm = _convection_storm_score(features)
    timing = compute_storm_timing_factor(features)
    context = _build_prediction_context(features, storm, mu, sigma, timing)
    _cache_model(mu, sigma, features.get("hour_of_day", 12))

    # Live brackets = the same markets the live dashboard prices against.
    event_date_str, markets = None, []
    error = None
    try:
        event_date_str, markets = find_live_event()
    except Exception as e:  # noqa: BLE001
        error = str(e)

    trades = []
    if markets:
        try:
            trades = evaluate_polymarket_brackets(
                event_date_str, mu, sigma, markets,
                todays_max_so_far=features.get("wsss_todays_max_so_far"),
                hour_of_day=features.get("hour_of_day"),
                live_prices=feed.snapshot(),
            )["trades"]
        except Exception as e:  # noqa: BLE001
            error = str(e)

    # --- Monte Carlo: simulate the actual day settling N times ---
    N = 5000
    draws = np.random.default_rng().normal(mu, sigma, N)

    low_lo = math.floor(mu - 2.5 * sigma - 0.5)
    low_hi = math.ceil(mu + 2.5 * sigma + 0.5)
    edges = np.arange(low_lo, low_hi + 0.5, 0.5)
    hist = []
    for i in range(len(edges) - 1):
        cnt = int(((draws >= edges[i]) & (draws < edges[i + 1])).sum())
        hist.append({"lo": round(float(edges[i]), 1), "hi": round(float(edges[i + 1]), 1), "count": cnt})

    mc = {
        "n": N,
        "mean_c": round(float(np.mean(draws)), 3),
        "std_c": round(float(np.std(draws)), 3),
        "p10": round(float(np.percentile(draws, 10)), 2),
        "p50": round(float(np.percentile(draws, 50)), 2),
        "p90": round(float(np.percentile(draws, 90)), 2),
        "histogram": hist,
    }

    # Per-bracket Monte-Carlo resolution.
    from execution.polymarket import parse_temperature_bounds  # local import, cold path

    for t in trades:
        try:
            low, high = parse_temperature_bounds(t["bracket"])
        except Exception:  # noqa: BLE001
            continue
        if low is None or high is None:
            continue
        win = int(((draws >= low) & (draws < high)).sum())
        t["mc_win_rate"] = round(win / N, 4)
        ask = _f(t.get("price"), 0) or 0
        stake = _f(t.get("stake_usd"), 0.0) or 0.0
        # E[PnL] = win * (1 - ask) * stake - loss * ask * stake
        t["mc_exp_pnl"] = round((win / N) * (1.0 - ask) * stake - (1.0 - win / N) * ask * stake, 3)
        # Finite display bounds (JSON has no infinity).
        t["mc_low"] = round(max(low, mu - 4.0 * sigma), 2)
        t["mc_high"] = round(min(high, mu + 4.0 * sigma), 2)

    return JSONResponse(content={
        "generated_at_sgt": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT"),
        "scenario": features,
        "prediction": {
            "mean_c": round(mu, 2),
            "std_c": round(sigma, 2),
            "storm_score": round(storm, 3),
            "signal_state": _build_signal_state(features, trades),
        },
        "context": context,
        "event_date_str": event_date_str,
        "error": error,
        "trades": trades,
        "mc": mc,
    })


# Serve the static SPA from the same origin (must be last).
app.mount("/", StaticFiles(directory="static", html=True), name="static")
