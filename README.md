# SG Max-Temp Betting Model

## What's in here

```
src/
  ingest.py          NEA data.gov.sg real-time API client (island-wide stations)
  wrh_scraper.py      weather.gov WRH scraper for WSSS/Changi METAR (single station,
                       matches what you've been reading directly) + SQLite storage
  api.py               FastAPI service: scrape -> store -> query, incl. live
                       "today's rolling max" endpoint for betting use
  synth_data.py       Synthetic 10-year 5-min dataset mimicking SG diurnal +
                       convective-cap dynamics (placeholder until real history
                       is backfilled -- see below)
  features.py          Snapshot-resampling feature engineering (the "many rows
                       per day" trick that lets one model serve every time-of-day)
  train_backtest.py    Walk-forward LightGBM quantile (P5/P50/P95) training +
                       calibration/trigger/baseline backtest
  conformal.py          Split-conformal correction layer that fixes quantile
                       overconfidence (see results below)
data/
  samples/wsss_sample.html   Real HTML snippet (from your paste) used to
                              validate the scraper parser
```

## IMPORTANT: what actually ran vs what's real

This was built in a sandboxed dev environment with network access locked to
package repos only (pypi, npm, github) -- **it cannot reach `data.gov.sg` or
`weather.gov`**. Two different things were validated as a result:

1. **The scraper's parsing logic is validated against your real pasted HTML**
   (`wrh_scraper.py` correctly parsed all fields, including the `8G20` wind-gust
   format and the `Lt thunder shwr` weather text, from the actual WSSS table).
   The `requests.get()` calls themselves are untested live -- run
   `python src/wrh_scraper.py` or hit `POST /scrape/latest` once you're outside
   this sandbox to confirm connectivity.

2. **The training/backtest pipeline was run end-to-end on synthetic data**
   (`synth_data.py`), not your real METAR history. The synthetic generator
   encodes the dynamics you described (morning warm-up, late-morning/early-
   afternoon peak, convective cap when rain hits) so the pipeline mechanics
   are proven to work -- but the actual numbers below are NOT a real backtest
   of Singapore weather. Treat this as "the code runs and does the right
   thing," not "here is your model's real skill."

**Update after your live test run (`python src/ingest.py --test` on your
machine):** air_temperature, rainfall, relative_humidity, and wind_speed/
direction worked immediately. Three bugs surfaced and are now fixed in
`ingest.py`:
- **UV index 403**: the real endpoint path is `/v2/real-time/api/uv`, not
  `/uv-index` as originally guessed. Fixed.
- **PM2.5 silently returning 0 rows**: PM2.5/PSI/UV use a different, *region*-
  based response schema (`data.regionMetadata` + `data.items[].readings`,
  one value per region: national/east/west/north/south/central) than the
  station-based schema air-temp/rainfall/humidity/wind use. The original
  parser only handled the station schema. Added a separate
  `fetch_region_reading()` for these three.
- **PSI 429 (rate limited)**: anonymous requests are rate-limited when you
  hit several endpoints back-to-back. Added retry-with-backoff on 429, plus
  a small pacing delay between calls in `fetch_all_current()`. If you hit
  this often, sign up for a free API key on data.gov.sg for higher limits.

**One thing still worth confirming on your machine**: the UV response's
internal `readings` key (`_REGION_READING_KEY["uv_index"] = "index"` in
`ingest.py`) is a best guess -- NEA's UV payload shape is documented less
consistently than PM2.5/PSI. Run `fetch_region_reading("uv_index")` and
`print()` the raw payload once; if the key doesn't match, it's a one-line fix.

## What the synthetic backtest showed (mechanics proof, not real skill)

Raw LightGBM quantile model, walk-forward trained 2016-2025, tested 2021-2026:

| Metric | Value |
|---|---|
| Claimed P5-P95 coverage | 90% |
| **Empirical coverage (raw)** | **86.1%** -- overconfident |
| Mean raw interval width | 0.71°C |
| Trigger-moment hit rate (width ≤ 1.5°C) | 86.7% |
| Persistence baseline MAE | 0.92°C |

This is exactly the failure mode flagged during design: **quantile regression
models are routinely overconfident**, so claiming "90%" without checking is
risky. Run `python src/conformal.py` to see the split-conformal correction
close that gap (widens intervals using a held-out calibration fold so
empirical coverage tracks the claimed 90%).

## Next steps to make this real

1. **Backfill real history.** Two options, can combine:
   - `wrh_scraper.fetch_wsss_range(start, end)` — pull years of WSSS METAR
     from weather.gov's history mode (30-min resolution, single station,
     exactly matches what you read manually).
   - `ingest.fetch_reading(...)` — NEA island-wide stations (5-min, multi-
     station spatial gradient, plus rainfall/lightning/UV that METAR doesn't
     carry). Useful as *additional* features even if WSSS is your primary
     target series.
   Run both outside this sandbox; each script's `__main__` block is a working
   entry point.

2. **Rebuild `features.py`'s training table from real data** instead of
   `synth_data.py`'s output — same function, just point it at your real
   5-min or 30-min joined WSSS+NEA table.

3. **Rerun `train_backtest.py` + `conformal.py` on real data** and read the
   calibration report for real. Pay attention to the **regime-stratified
   breakdown** (dry vs rain days, monsoon season) mentioned in our design
   discussion — that's not yet built here; the current backtest only reports
   aggregate + time-of-day coverage. Worth adding once real data is in, since
   aggregate calibration can hide a model that's bad specifically in the
   regime you're betting on.

4. **Wire the live loop**: poll `wrh_scraper.fetch_wsss_latest()` +
   `ingest.fetch_all_current()` every 5-15 min, run through `features.py`
   (single-row, snapshot=now), predict with the trained+calibrated models,
   check against your bet-trigger width threshold.

## Honest limitation to keep in view

Conformal calibration gives *marginal* coverage guarantees under the
assumption that calibration and test data are exchangeable — which is not
quite true across monsoon regime shifts or unusual years (e.g. a strong
El Niño year). The walk-forward + regime-stratified backtest is what tells
you whether that assumption is holding up in practice; don't trust the
90% number without checking it regime-by-regime on real data first.

## Setup

```bash
pip install lightgbm pandas numpy scikit-learn requests pyarrow \
            beautifulsoup4 lxml fastapi uvicorn httpx --break-system-packages

# validate scraper against real sample
python src/wrh_scraper.py

# generate synthetic data + run full pipeline (proof of mechanics)
python src/synth_data.py
python src/features.py
python src/train_backtest.py
python src/conformal.py

# run the API
uvicorn src.api:app --reload --port 8000
```
