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

import math
import time
from contextlib import asynccontextmanager
from datetime import datetime


def _f(v, default: float):
    try:
        f = float(v)
        return f if f == f else default  # NaN check
    except (TypeError, ValueError):
        return default

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from data.config import ENTRY_WINDOW_HOURS
from data.feature_engine import extract_singapore_feature_vector
from data.ingestion import fetch_all_data_gov, fetch_wsss_metar_history
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

# ---------------------------------------------------------------------------
# Global feed + advisory position book — started/stopped by lifespan below.
# ---------------------------------------------------------------------------
feed = LiveFeed()
book = PositionBook()


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
    # Log exits when positions settle (take-profit / stop / resolved)
    closed = book.settle_actions()
    for c in closed:
        log_exit(c["bracket"], c["side"], c["exit_price"], c["pnl_pct"], c["closed_action"])


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    feed.start()
    yield
    feed.stop()


app = FastAPI(title="SG Max-Temp Dashboard", version="1.0.0", lifespan=lifespan)

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
    context = _build_prediction_context(features, storm, mu, sigma)

    # 4-5. Find the live Polymarket event and price the brackets.
    event = None
    try:
        event_date_str, markets = find_live_event()
        if markets:
            result = evaluate_polymarket_brackets(event_date_str, mu, sigma, markets)
            trades = result["trades"]
            # When-to-trade: turn a promising bracket into an advisory book entry
            # (and fold any already-open position's manage signals in).
            hour_of_day = features.get("hour_of_day", 12)
            _run_entry_gate(trades, hour_of_day, feed.snapshot(), features)
            event = {
                "date_str": result["event_date_str"],
                "trades": trades,
            }
        else:
            event = {"date_str": event_date_str, "error": "No open event found within the lookahead window."}
    except Exception as e:  # noqa: BLE001
        event = {"date_str": None, "error": str(e)}

    payload = {
        "generated_at_sgt": generated_at_sgt,
        "prediction": {
            "mean_c": round(mu, 2),
            "std_c": round(sigma, 2),
            "hour_of_day": features.get("hour_of_day"),
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
    return {
        "status": "ok",
        "server_time_sgt": datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT"),
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


# Serve the static SPA from the same origin (must be last).
app.mount("/", StaticFiles(directory="static", html=True), name="static")
