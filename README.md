# Max-Temp Prediction Market Dashboard

A weather-aware **prediction-market terminal**. It forecasts the daily maximum
temperature from live meteorological feeds and turns that forecast into
actionable trading signals for a mutually-exclusive, all-or-nothing temperature
bracket market.

It does **not** place real orders — this is **decision support**. It watches real
market quotes, estimates the model's edge on each bracket, and runs an advisory
position book showing what it *would* enter, when to take profit, and when to
stop. Real execution requires exchange credentials and is a separate follow-on.

## How it works

```
weather feeds ──► features ──► max-temp distribution ──► bracket edge ──► advisory book
```

1. **Ingest** (`data/ingestion.py`) — pulls live conditions from a national
   weather service's public open-data API and the single-site airport METAR
   feed (30-min resolution, the same surface observations a manual reader
   would follow).
2. **Features** (`data/feature_engine.py`) — builds a snapshot feature vector:
   current temp, running max so far today, dew point, wind, UV, lightning,
   rain-station ratio, and METAR freshness.
3. **Predict** (`main.py`) — fits a smooth diurnal heating curve so the
   forecasted max and its uncertainty respond continuously to time-of-day,
   UV, storm cover, and data staleness (pre-dawn wide, settled near the
   afternoon/night peak).
4. **Price** (`execution/polymarket.py`) — pulls live bracket quotes and
   computes the edge (model probability minus ask). YES and NO are priced on
   the same bracket for both sides.
5. **When-to-trade + sizing** (`execution/kelly_sizer.py`, `data/config.py`) —
   flat stake per position, with a **hard per-trade payout cap kept in the
   2–8% band**, and a timing gate that only enters during a defined daily
   window (or a near-settled lockout case).
6. **Advisory book** (`execution/positions.py`) — tracks open positions with
   live P&L from sell prices and auto-suggests `TAKE_PROFIT` / `STOP` / `HOLD`.
   Every entry, exit, and signal is logged to local history
   (`execution/trade_history.py`).

## Run it

```bash
python3 -m pip install -r requirements.txt     # use python3 -m pip, not bare pip
python3 -m uvicorn server:app --port 8000
```

Open <http://localhost:8000> — a live dashboard shows the prediction curve,
current conditions, the priced brackets, the advisory book, and the signal log.

### API

| Endpoint | What it returns |
|---|---|
| `GET /api/health` | lightweight health + feed telemetry |
| `GET /api/dashboard` | full pipeline: features + prediction + brackets + positions |
| `GET /api/prices` | live bracket prices (instant, from the WebSocket feed) |
| `GET /api/positions` | advisory position book with live P&L and exit action |
| `GET /api/history` | recent signals / trades with local timestamps |

## Layout

```
main.py                 prediction pipeline (diurnal curve, mu/sigma)
server.py               FastAPI backend (dashboard + prices + book + history)
data/
  config.py             model + trading constants
  feature_engine.py     snapshot feature vector
  ingestion.py          weather + METAR ingestion
execution/
  kelly_sizer.py        flat sizing + 2–8% payout cap
  polymarket.py         live bracket quotes + edge
  positions.py          advisory position book
  ws_feed.py            live price WebSocket daemon
  trade_history.py      local trade log
static/                 the dashboard UI
```

## Honest scope

This is a forecasting + decision-support harness. The numbers it produces are
its own model's view of edge; they are **not a guarantee of outcome** and they
do not place orders. Core assumptions to keep in view: calibration degrades
under regime shifts (e.g. unusual weather years), and the single-site METAR
feed is one point source. Treat the dashboard as a live monitor and a starting
point, not a signal to deploy capital blindly.
