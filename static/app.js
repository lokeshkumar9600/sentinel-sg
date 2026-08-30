/* Max-Temp dashboard — polls the FastAPI backend and renders the result. */

const POLL_INTERVAL_FULL = 30;  // seconds: full weather pipeline (slower, heavier)
const POLL_INTERVAL_PRICES = 0.1; // seconds: Polymarket prices only (fast, 10 req/s)

const $ = (id) => document.getElementById(id);

const els = {
  conn: $("conn-pill"),
  clock: $("clock"),
  refresh: $("refresh-btn"),
  updated: $("last-updated"),
  countdown: $("countdown"),
  eventNote: $("event-note"),
  predValue: $("pred-value"),
  predStderr: $("pred-stderr"),
  predChart: $("pred-chart"),
  conditions: $("conditions"),
  bracketsBody: $("brackets-body"),
  bracketsFoot: $("brackets-foot"),
  positionsBody: $("positions-body"),
  positionsFoot: $("positions-foot"),
  feedStatus: $("feed-status"),
  errorbar: $("errorbar"),
};

let countdown = POLL_INTERVAL_FULL;

/* ---------------- clock + countdown ---------------- */

function tickClock() {
  const now = new Date();
  const sgt = new Date(now.toLocaleString("en-US", { timeZone: "Asia/Singapore" }));
  els.clock.textContent = sgt.toLocaleTimeString("en-US", { hour12: false }) + " SGT";
}

function tickCountdown() {
  els.countdown.textContent = countdown;
  if (countdown <= 0) {
    countdown = POLL_INTERVAL_FULL;
    loadDashboard();  // full pipeline at slower interval
  }
  countdown -= 1;
}

/* ---------------- rendering ---------------- */

function setConn(state, text) {
  els.conn.dataset.state = state;
  els.conn.textContent = text;
}

function renderConditions(f) {
  const tiles = [
    ["Current temp", fmt(f.wsss_current_temp, "°C")],
    ["Max so far today", fmt(f.wsss_todays_max_so_far, "°C")],
    ["Spatial prox temp", fmt(f.spatial_changi_prox_temp, "°C")],
    ["Dew point", fmt(f.wsss_dewp, "°C")],
    ["Wind speed", fmt(f.wsss_wspd, " kt")],
    ["UV index", fmt(f.uv_index, "")],
    ["Lightning strikes", intString(f.lightning_strike_count)],
    ["Stations raining", pct(f.rain_station_ratio)],
    ["METAR age", staleMark(f.minutes_since_last_metar)],
  ];
  els.conditions.innerHTML = tiles
    .map(([label, value]) => `<div class="cond"><div class="cond__label">${label}</div><div class="cond__value">${value}</div></div>`)
    .join("");
}

function fmt(v, unit) {
  if (v == null || Number.isNaN(v)) return "—";
  return Number(v).toFixed(1) + unit;
}
function intString(v) { return v == null ? "—" : Number(v).toLocaleString(); }
function pct(v) { return v == null ? "—" : Math.round(v * 100) + "%"; }
function staleMark(min) {
  if (min == null) return "—";
  if (min >= 120) return `<span style="color:var(--serious)">${Math.round(min)}m (stale)</span>`;
  return Math.round(min) + "m";
}

function renderAction(trade) {
  const cls = (trade.action || "").toUpperCase();
  return `<span class="tag tag--${cls}">${escapeHtml(cls)}</span>`;
}

/* Advisory position book: what the model has entered and when to exit. */
function renderPositions(positions) {
  const el = els.positionsBody;
  if (!el) return;
  if (!positions || !positions.length) {
    el.innerHTML = `<tr><td colspan="6" class="emptystate">No open positions</td></tr>`;
    if (els.positionsFoot) els.positionsFoot.textContent = "";
    return;
  }
  els.positionsBody.innerHTML = positions
    .map((p) => {
      const entry = p.entry_price != null ? "¢" + (p.entry_price * 100).toFixed(1) : "—";
      const now = p.exit_price != null ? "¢" + (p.exit_price * 100).toFixed(1) : "—";
      const pnl = p.pnl_pct != null ? (p.pnl_pct * 100).toFixed(1) + "%" : "—";
      const pnlCls = !p.pnl_pct ? "" : p.pnl_pct >= 0 ? "yes" : "no";
      return `<tr>
        <td>${escapeHtml(p.bracket || "")}</td>
        <td>${renderAction({ action: p.side })}</td>
        <td class="num">${entry}</td>
        <td class="num">${now}</td>
        <td class="num ${pnlCls}">${pnl}</td>
        <td>${renderAction(p)}</td>
      </tr>`;
    })
    .join("");
  if (els.positionsFoot) {
    els.positionsFoot.textContent = `${positions.length} open position(s) · exit when sell ≥ entry + profit band or ≤ entry − stop`;
  }
}

function renderBrackets(event) {
  if (!event || event.error) {
    els.bracketsBody.innerHTML = `<tr><td colspan="7" class="emptystate">${escapeHtml((event && event.error) || "No event data.")}</td></tr>`;
    els.bracketsFoot.textContent = "";
    return;
  }
  const trades = event.trades || [];
  els.bracketsFoot.textContent = `Event ${event.date_str} · ${trades.length} bracket(s) · buy price = best ask (¢ per $1 payout)`;
  if (!trades.length) {
    els.bracketsBody.innerHTML = `<tr><td colspan="7" class="emptystate">No open brackets.</td></tr>`;
    _lastModelProbs = [];
    return;
  }
  _lastModelProbs = trades.map((t) => (t.prob != null ? t.prob : null));
  _prevYes = trades.map((t) => (t.yes_price != null ? t.yes_price * 100 : null));
  _prevNo = trades.map((t) => (t.no_price != null ? t.no_price * 100 : null));
  els.bracketsBody.innerHTML = trades
    .map((t) => {
      const stake = t.stake_usd != null ? "$" + Number(t.stake_usd).toFixed(2) : "—";
      const prob = t.prob != null ? (t.prob * 100).toFixed(1) + "%" : "—";
      return `<tr>
        <td>${escapeHtml(t.bracket || "")}</td>
        <td class="num">${prob}</td>
        <td class="num yes">${t.yes_price != null ? "¢" + (t.yes_price * 100).toFixed(1) : "—"}</td>
        <td class="num no">${t.no_price != null ? "¢" + (t.no_price * 100).toFixed(1) : "—"}</td>
        <td>${renderAction(t)}</td>
        <td class="num">${t.edge != null ? (t.edge * 100).toFixed(1) + "%" : "—"}</td>
        <td class="num">${stake}${t.scaled_down ? " *" : ""}</td>
      </tr>`;
    })
    .join("");
}

/* Normal-distribution curve (single series, sequential blue). */
function renderChart(prediction) {
  const W = 480, H = 220, padL = 46, padR = 12, padT = 14, padB = 30;
  const mu = prediction.mean_c, sigma = Math.max(0.15, prediction.std_c);
  const lo = mu - 3.4 * sigma, hi = mu + 3.4 * sigma;
  const xs = (t) => padL + ((t - lo) / (hi - lo)) * (W - padL - padR);
  const ymax = 1 / (sigma * Math.sqrt(2 * Math.PI));
  const ys = (y) => H - padB - (y / ymax) * (H - padT - padB);
  const pdf = (x) => Math.exp(-0.5 * ((x - mu) / sigma) ** 2) / (sigma * Math.sqrt(2 * Math.PI));

  const n = 120;
  let line = "";
  for (let i = 0; i <= n; i++) {
    const x = lo + (i / n) * (hi - lo);
    line += (i ? " L" : "M") + xs(x).toFixed(1) + " " + ys(pdf(x)).toFixed(1);
  }
  const area = line + ` L${xs(hi).toFixed(1)} ${ys(0).toFixed(1)} L${xs(lo).toFixed(1)} ${ys(0).toFixed(1)} Z`;

  // Ticks every 1°C
  let ticks = "";
  for (let t = Math.round(lo); t <= Math.round(hi); t++) {
    if (t < lo || t > hi) continue;
    const x = xs(t);
    ticks += `<line x1="${x.toFixed(1)}" y1="${H - padB}" x2="${x.toFixed(1)}" y2="${H - padB + 5}" stroke="var(--baseline)" stroke-width="1"/>`;
    ticks += `<text x="${x.toFixed(1)}" y="${H - padB + 17}" font-size="10" fill="var(--muted)" text-anchor="middle">${t}</text>`;
  }

  els.predChart.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Predicted max temperature follows a normal distribution centered at ${mu.toFixed(1)}°C">
      <line x1="${padL}" y1="${H - padB}" x2="${W - padR}" y2="${H - padB}" stroke="var(--baseline)" stroke-width="1"/>
      ${ticks}
      <path d="${area}" fill="var(--series-1)" opacity="0.18" stroke="none"/>
      <path d="${line}" fill="none" stroke="var(--series-1)" stroke-width="2.5" stroke-linecap="round"/>
      <line x1="${xs(mu).toFixed(1)}" y1="${padT}" x2="${xs(mu).toFixed(1)}" y2="${H - padB}" stroke="var(--series-1)" stroke-width="1.5" stroke-dasharray="3 3"/>
      <text x="${xs(mu).toFixed(1)}" y="${padT - 6}" font-size="11" fill="var(--text-primary)" text-anchor="middle" font-weight="600">${mu.toFixed(1)}°C</text>
      <text x="${padL + 4}" y="${padT + 2}" font-size="10" fill="var(--muted)">σ=${sigma.toFixed(2)}°C</text>
    </svg>`;
}

/* ---------------- load: full dashboard (slower) ---------------- */

async function loadDashboard() {
  els.refresh.disabled = true;
  els.errorbar.hidden = true;
  setConn("loading", "Loading…");
  try {
    const res = await fetch("/api/dashboard", { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    els.updated.textContent = data.generated_at_sgt || "—";
    renderConditions(data.features || {});
    renderChart(data.prediction || { mean_c: 0, std_c: 1 });
    els.predValue.textContent = data.prediction ? data.prediction.mean_c.toFixed(1) + "°C" : "—";
    els.predStderr.textContent = data.prediction
      ? `±${data.prediction.std_c.toFixed(2)}°C · hour ${data.prediction.hour_of_day ?? "—"} SGT`
      : "";
    renderBrackets(data.event);
    renderPositions(data.positions);
    els.eventNote.textContent = "Event: " + ((data.event && data.event.date_str) || "none");
    setConn("ok", "Live");
  } catch (err) {
    setConn("error", "Unreachable");
    els.errorbar.hidden = false;
    els.errorbar.textContent = "Could not reach backend (" + err.message + "). Is uvicorn running on :8000?";
  } finally {
    els.refresh.disabled = false;
  }
}

/* ---------------- load: Polymarket prices only (fast, 0.3s) ---------------- */

/* Sequential guard: at 0.1s polling, two requests can overlap (Gamma fetch ~90ms).
   Dropping stale responses prevents an older price landing after a newer one. */
let _priceSeq = 0;
async function loadPrices() {
  const seq = ++_priceSeq;
  try {
    const res = await fetch("/api/prices", { cache: "no-store" });
    if (!res.ok) return;
    if (seq < _priceSeq) return;  // a newer request already answered - discard
    const data = await res.json();
    // Update only the prices in the brackets table, not the full re-render.
    bumpPriceTicks();
    if (data.positions) renderPositions(data.positions);
    if (data.brackets && data.brackets.length) {
      renderPricesOnly(data.brackets);
    } else {
      updateFeedStatus();
    }
  } catch (err) {
    // Silent fail on prices — not critical enough to show error.
  }
}

/* Store the last model probabilities so fast price updates can recalculate edge/action. */
let _lastModelProbs = [];  // parallel array to bracket rows
// Last-rendered cents for change detection (YES buy and NO buy columns).
let _prevYes = [], _prevNo = [];

/* ---------------- live feed telemetry (proves the 0.1s loop is alive) ---------------- */
let _priceTicks = 0;       // successful price responses since page load
let _lastMoveAt = null;    // Date of the most recent actual price change

function updateFeedStatus() {
  const el = els.feedStatus;
  if (!el) return;
  const hz = (POLL_INTERVAL_PRICES).toFixed(1);
  const last = _lastMoveAt
    ? " · last move " + _lastMoveAt.toLocaleTimeString("en-US", { hour12: false }) + "." +
      String(_lastMoveAt.getMilliseconds()).padStart(3, "0")
    : " · no moves logged yet";
  el.textContent = `price feed ${hz}s · ${_priceTicks} ticks live` + last;
  el.dataset.moved = _lastMoveAt ? "yes" : "no";
}

/* Smooth flash when a price actually shifts: bg brightens up/down then eases back. */
function _flashCell(cell, dir) {
  cell.classList.remove("flash-up", "flash-down");
  void cell.offsetWidth;            // restart the transition
  cell.classList.add(dir === "up" ? "flash-up" : "flash-down");
  clearTimeout(cell._flashT);
  cell._flashT = setTimeout(() => cell.classList.remove("flash-up", "flash-down"), 550);
}

function _patchPrice(cell, cents, prev) {
  const txt = cents != null ? "¢" + cents.toFixed(1) : "—";
  if (cell.textContent === txt) return;   // unchanged - nothing to animate
  const old = prev != null ? prev : null;
  if (old != null && cents != null && cents !== old) {
    _flashCell(cell, cents > old ? "up" : "down");
    _lastMoveAt = new Date();
  }
  cell.textContent = txt;
}

/* Patch the YES buy + NO buy price columns + re-evaluate edge/action in-place.
   Uses stored model probs from the last full dashboard render. Sell prices
   (yes_sell / no_sell) continue to flow through the API for future risk-mgmt
   features but are intentionally not shown here. Column map:
   0 Bracket · 1 Model prob · 2 Yes buy · 3 No buy · 4 Action · 5 Edge · 6 Stake */
function renderPricesOnly(brackets) {
  const rows = els.bracketsBody.querySelectorAll("tr");
  brackets.forEach((b, i) => {
    if (rows[i]) {
      const cells = rows[i].querySelectorAll("td");
      // A $1 payout caps at 1.0, so the upper bound is inclusive: a bracket
      // priced right at the ceiling (e.g. a NO that costs ¢100) is a real quote
      // and should display rather than being hidden as "invalid".
      const yes = b.yes != null && b.yes > 0 && b.yes <= 1 ? b.yes : null;
      const no = b.no != null && b.no > 0 && b.no <= 1 ? b.no : null;
      const yesC = yes != null ? yes * 100 : null;
      const noC = no != null ? no * 100 : null;
      if (cells[2]) _patchPrice(cells[2], yesC, _prevYes[i]);
      if (cells[3]) _patchPrice(cells[3], noC, _prevNo[i]);
      _prevYes[i] = yesC; _prevNo[i] = noC;

      // Re-evaluate edge + action from the stored model probability (buy side).
      const modelProb = _lastModelProbs[i];
      if (modelProb != null) {
        const edge = yes != null ? modelProb - yes : null;
        if (cells[5]) cells[5].textContent = edge != null ? (edge * 100).toFixed(1) + "%" : "—";
        let action = "SKIP";
        if (yes != null && edge != null) {
          if (edge >= 0.08) action = "BUY_YES";
          else if (no != null && (1 - modelProb) - no >= 0.08) action = "BUY_NO";
        }
        if (cells[4]) cells[4].innerHTML = renderAction({ action });
      }
    }
  });
  updateFeedStatus();
}

/* Count every successful price response so the tick counter is accurate. */
function bumpPriceTicks() { _priceTicks++; }

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ---------------- boot ---------------- */

// Two separate polling loops:
// - Full dashboard (weather + prediction + trades): every 30s
// - Polymarket prices only: every 0.1s (10 req/s, well under Gamma's 15k/10s limit)
setInterval(tickClock, 1000);
setInterval(tickCountdown, 1000);
els.refresh.addEventListener("click", () => { countdown = POLL_INTERVAL_FULL; loadDashboard(); });
tickClock();
loadDashboard();       // immediate full load
setInterval(loadPrices, POLL_INTERVAL_PRICES * 1000);  // fast price refresh
