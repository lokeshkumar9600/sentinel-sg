# Sentinel — How Hot Will Singapore Get Today? (Bet smarter.)

> Sentinel watches live weather + market prices, predicts Singapore's daily
> maximum temperature, and tells you — hours in advance — which temperature
> bracket is most likely to win in a prediction market. It's a decision-support
> dashboard, not a money-bot.

---

## 🧭 For a first-time reader

**The idea, in plain words.**

Every day there's a market where people bet on Singapore's daily maximum
temperature, in 1°C brackets ("32°C", "33°C", "34°C", …). Only one bracket
wins: the one that contains the actual high temperature recorded that day.

Sentinel tries to predict the winner **early in the day**, when the market odds
are still loose and good bets exist. It:

1. **Reads the weather** — live temperature, dew point, wind, UV, lightning,
   rainfall, cloud, and the airport's official METAR station report.
2. **Forecasts the max** — fits a smooth "sun is heating the island" curve that
   tightens as the day goes on. Pre-dawn it's unsure (wide range); by
   mid-afternoon it's usually nailed within ~±0.5°C.
3. **Prices the market** — compares its forecast probability for each bracket
   against the actual buy/sell prices on the market, and computes the "edge"
   (how much better the model's odds are than what the market is asking).
4. **Suggests trades** — as an *advisory book*: "buy 33°C YES at $0.53",
   then later "take profit", or "stop". It manages position size, daily loss
   limits, and when not to trade.
5. **Shares its scorecard** — a dashboard, history log, backtest, and
   "how soon did the model call it" analysis that keeps the model honest.

> ⚠️ **Important:** Sentinel does **not** place real orders. It watches real
> prices and shows what it *would* do. It's a monitor and a decision-aid — you
> decide whether to act on it.

---

## ✨ What you get

| Page | What it does |
|---|---|
| **Dashboard** | Live forecast curve, current weather, each bracket + price + model edge, your advisory position book, and the signal feed — all in real time |
| **History & Performance** | Every past day: how soon the model locked onto the winning bracket, whether it held, and whether it traded. Plus the complete signal/trade log |
| **Simulations** | Re-run the trading model over any past date range to see how the strategy would have behaved |
| **Backtest** | Per-day "predicted vs actual" accuracy across settled days |
| **Analytics** | Model error stats (MAE, bias, ±1σ hit rate), calibration, and live price-feed health |

---

## 🚀 Run it (2 ways)

### Option A — Docker (easiest, recommended)

```bash
cp .env.example .env            # 1) make your config (edit receivers — see below)
docker compose up --build       # 2) build + start
```

Then open **http://localhost:8000**. Your trade history and predictions are
stored in a Docker volume, so they survive restarts.

### Option B — Python directly

```bash
python3 -m pip install -r requirements.txt     # note: python3 -m pip, not pip
python3 -m uvicorn server:app --port 8000
```

Open **http://localhost:8000**.

> Your one required config step is optional: the app runs fine with no keys. Add
> a data.gov.sg key for higher API rate limits, and set up alerting below if you
> want to be notified of signals.

---

## 🔔 Get alerted when the bot signals

The app always logs every signal on-screen (Dashboard + History pages) — alerts
are an optional extra push.

**Email (recommended for a single owner)** — in `.env`:

```ini
NOTIFY_EMAIL_RECIPIENTS=lokesh960077152@gmail.com   # who gets alerts
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your-gmail@gmail.com                       # the sending account
SMTP_PASSWORD=your-app-password                      # not your login password!
SMTP_USE_SSL=false
```

For Gmail you must use an **App Password** (not your normal login password):
turn on 2-Step Verification, then create one at
<https://myaccount.google.com/apppasswords>. You can send to your own inbox.

Test it with one command:

```bash
python3 -m execution.notifications    # sends a TEST email
```

**Webhook (Discord / Slack / Ntfy)** — set `TRADE_WEBHOOK_URL` in `.env` to a
channel URL. In Discord: *Server Settings → Integrations → Webhooks → New
Webhook* → copy URL → paste it in. Each signal then posts there within seconds.

---

## 🔌 API (for the curious)

The dashboard is just a browser on these endpoints — you can poke them yourself:

| Endpoint | Returns |
|---|---|
| `GET /api/health` | is the app + data feed alive |
| `GET /api/dashboard` | everything: weather features, forecast, brackets, positions |
| `GET /api/prices` | live live bracket prices (WebSocket) |
| `GET /api/positions` | advisory position book with P&L + suggested exit action |
| `GET /api/history` | recent signals / trades |
| `GET /api/backtest` | per-day predicted vs actual accuracy |
| `GET /api/early_prediction` | "how soon did the model call it" analysis |
| `GET /api/performance` | model error stats (MAE, bias, hit rate) |
| `GET /api/export` / `GET /api/export/csv` | download all trade history |
| `GET /api/simulate` | replay the model over a date range |

---

## 🗂️ Project layout (short version)

```
main.py             forecast engine (temperature curve → probability)
server.py           the API + dashboard backend
Dockerfile          container build
docker-compose.yml  one-command deploy with persistent storage
data/               weather ingestion, features, analytics, self-tuning model
execution/          market pricing, advisory positions, trade log, alerts
static/             the web UI (dashboard, history, backtest, analytics)
tests/              automated tests
.github/            CI pipeline
```

---

## ✅ Quality

- Automated test suite (`python3 -m pytest`) runs in CI on every push.
- Self-tuning: the model learns from each settled day (bias/sigma calibration).
- Guard rails on the advisory book: fair-value exits, per-day loss limits,
  stop-loss cooldowns, and a "don't trade when the market is too thin" rule.

## 🧂 Honest limitations

- It's a **forecast + decision aid**, not an order-placement bot.
- The forecast is only as good as its inputs: calibration can drift in unusual
  weather years, and it relies on a single airport weather station.
- Trade history on the **Render free tier** is ephemeral — it resets on each
  redeploy. For persistent history, run the Docker setup with its data volume.

---

*Built for the Singapore daily max-temperature prediction market. Treat the
numbers as a starting point for your own judgment, not a promise of outcomes.*