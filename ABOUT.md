# About — for the GitHub repo

Copy the short description into the repo's **About → Description** box
(Settings → also shown at the top-right of the repo page when you click the
pencil ✏️ next to "About").

## Short description (for the About box — 320 chars, under the 350 limit)

Forecasts Singapore's daily max temperature from live weather and the airport
METAR, prices every bracket against the prediction market, and runs an advisory
trading book with profit-taking, stops, and loss limits. Live dashboard,
backtests, email/webhook alerts, one-command Docker deploy. Decision support —
no real orders.

## Topics (tags for the repo)

```
prediction-market    weather-forecasting    temperature
singapore            metar-weather          fastapi
python               kelly-criterion        forecast
trading              backtesting            realtime-dashboard
data-engineerings    numerical-weather-prediction
```

(Pick ~5–10; GitHub allows up to 20.)

## Longer version (for a pinned gist / repo website / socials)

**Sentinel** answers one question hours before it matters: *how hot will
Singapore get today, and which temperature bracket wins the market?*

It fuses live government weather data (temperature, humidity, wind, UV,
lightning, rainfall, cloud) with the WSSS airport METAR — the exact station
the market settles on — into a daily-max forecast with honest uncertainty.
That forecast is priced bracket-by-bracket against live prediction-market
quotes, and a Kelly-sized advisory book recommends what to buy, when to take
profit, and when to stop — capped by payout and daily-loss limits.

Everything is observable: a real-time dashboard, a full signal/trade history,
per-day backtests, and a "how soon did the model call it" analysis that audits
whether early confidence actually holds. Email or webhook alerts push each
signal to you. Runs virtually anywhere via a single Docker command.

The catch, stated plainly: it is **decision support**, not an order bot. The
numbers are the model's view of edge — a starting point for your own judgment,
not a promise of outcomes.