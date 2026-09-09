/* Sentinel — Analytics page: time-series charts of METAR temps and model
   bracket probabilities. Both chart types use one small SVG line-chart builder
   so the geometry (margins, recessive grid, thin marks, hover layer) stays
   consistent across the page and can be reused elsewhere. */
const $ = (id) => document.getElementById(id);

const els = {
  clock: $('clock'),
  metarMeta: $('metar-meta'),
  metarChart: $('metar-chart'),
  predMeta: $('pred-meta'),
  predChart: $('pred-chart'),
  predLegend: $('pred-legend'),
  // latency panel
  latMeta: $('lat-meta'),
  latRate: $('lat-rate'),
  latRateNote: $('lat-rate-note'),
  latConn: $('lat-conn'),
  latAge: $('lat-age'),
  latTicks: $('lat-ticks'),
  latAvg: $('lat-avg'),
  latUp: $('lat-up'),
  latMetar: $('lat-metar'),
  latSpark: $('lat-spark'),
};

// --- latency state ----------------------------------------------------------
const _rateHistory = [];          // [{ t: Date, r10: number, r60: number }]
let _cachedCadence = null;        // seconds between METAR obs, from full analytics poll

// --- time helpers ---------------------------------------------------------
const HHMM = (ts) => String(ts || '').slice(11, 16); // "HH:MM"
const MON = (ts) => String(ts || '').slice(5, 10);   // "MM-DD"

function tickClock() {
  const now = new Date();
  const sgt = new Date(now.toLocaleString('en-US', { timeZone: 'Asia/Singapore' }));
  els.clock.textContent = sgt.toLocaleTimeString('en-US', { hour12: false }) + ' SGT';
}
setInterval(tickClock, 1000);
tickClock();

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// --- generic SVG line chart ----------------------------------------------
// plot({ svg, points, x: t => idx, yLabel, fmtY, seriesColor, tooltip })
// points: [{ t (label string), y }]
function plot(opts) {
  const svg = opts.svg;
  const W = 1000, H = opts.height || 220, padL = 44, padR = 16, padT = 16, padB = 26;
  const n = opts.points.length;
  const ys = opts.points.map(p => p.y).filter(v => v != null && isFinite(v));
  const yLo = Math.min(...ys), yHi = Math.max(...ys);
  const span = Math.max(yHi - yLo, opts.yMinSpan || 0.5);
  const yPad = opts.yPad ?? span * 0.18;
  const lo = yLo - yPad, hi = yHi + yPad;
  const xs = i => n > 1 ? padL + (i / (n - 1)) * (W - padL - padR) : padL + (W - padL - padR) / 2;
  const ys_ = v => H - padB - ((v - lo) / (hi - lo)) * (H - padT - padB);
  const col = opts.seriesColor || 'var(--accent)';

  // recessive gridlines + y labels
  let grid = '';
  for (let g = 0; g <= 4; g++) {
    const v = lo + (hi - lo) * g / 4;
    grid += `<line x1="${padL}" y1="${ys_(v).toFixed(1)}" x2="${W - padR}" y2="${ys_(v).toFixed(1)}" stroke="var(--grid)" stroke-width="1"/>`;
    grid += `<text x="${padL - 7}" y="${(ys_(v) + 3).toFixed(1)}" text-anchor="end" fill="var(--muted-ink)" font-size="9" font-family="var(--font-mono)">${opts.fmtY ? opts.fmtY(v) : v.toFixed(1)}</text>`;
  }

  // build the line from valid points; gap where a point is missing
  let line = '';
  let pending = null;
  opts.points.forEach((p, i) => {
    if (p.y == null || !isFinite(p.y)) { pending = null; return; }
    line += (pending === null ? ' M' : ' L') + xs(i).toFixed(1) + ' ' + ys_(p.y).toFixed(1);
    pending = i;
  });
  line = line.replace(/^ /, '');

  // area fill under the line
  const first = opts.points.findIndex(p => p.y != null && isFinite(p.y));
  const last = opts.points.length - 1;
  const area = line + ` L${xs(last).toFixed(1)} ${ys_(lo).toFixed(1)} L${xs(first).toFixed(1)} ${ys_(lo).toFixed(1)} Z`;

  // x labels: first / last + a handful in between, drawn at their true spot
  const step = Math.max(1, Math.ceil(n / 8));
  let xLabels = '';
  for (let i = 0; i < n; i++) {
    if (i !== 0 && i !== n - 1 && i % step !== 0) continue;
    const label = opts.xLabel ? opts.xLabel(opts.points[i]) : HHMM(opts.points[i].t);
    xLabels += `<text x="${xs(i).toFixed(1)}" y="${H - 8}" text-anchor="${i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle'}" fill="var(--muted-ink)" font-size="9" font-family="var(--font-mono)">${escapeHtml(label)}</text>`;
  }

  // hover layer: an invisible hit-target circle per point showing a native tooltip
  const hover = opts.points.map((p, i) => {
    if (p.y == null || !isFinite(p.y)) return '';
    return `<circle cx="${xs(i).toFixed(1)}" cy="${ys_(p.y).toFixed(1)}" r="9" fill="transparent" data-i="${i}" style="cursor:pointer">
      <title>${escapeHtml((opts.seriesLabel || '') + ' ' + p.t)} — ${opts.fmtY ? opts.fmtY(p.y) : p.y.toFixed(2)}</title>
    </circle>`;
  }).join('');

  // actual visible points (small dots) — only when not too dense
  const dotN = n > 400 ? 0 : n;
  let dots = '';
  for (let i = 0; i < dotN; i++) {
    const p = opts.points[i];
    if (p.y == null || !isFinite(p.y)) continue;
    dots += `<circle cx="${xs(i).toFixed(1)}" cy="${ys_(p.y).toFixed(1)}" r="1.6" fill="${col}"/>`;
  }

  svg.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" style="width:100%;height:100%">
      ${grid}
      <defs><linearGradient id="anArea" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="${col}" stop-opacity="0.22"/>
        <stop offset="1" stop-color="${col}" stop-opacity="0.02"/>
      </linearGradient></defs>
      <path d="${area}" fill="url(#anArea)"/>
      <path d="${line}" fill="none" stroke="${col}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      ${dots}
      ${xLabels}
      <g>${hover}</g>
    </svg>`;
  return { xs, ys: ys_, yRange: [lo, hi] };
}

// --- WSSS METAR temperature chart ----------------------------------------
function renderMetar(data) {
  const pts = data.metar || [];
  if (!pts.length) {
    els.metarMeta.textContent = 'no METAR data';
    els.metarChart.innerHTML = '';
    return;
  }
  els.metarMeta.textContent = pts.length + ' readings';
  plot({
    svg: els.metarChart,
    height: 230,
    points: pts.map(p => ({ t: p.ts_sgt, y: p.temp_c })),
    fmtY: v => v.toFixed(0) + '°',
    yPad: 1.5,
    yMinSpan: 2,
    seriesLabel: 'METAR',
  });
}

// --- model bracket predictions chart -------------------------------------
const PRED_COLORS = ['#4aa8ff', '#2ee8c0', '#ffb84d', '#ff6b8d', '#b388ff', '#8be9fd', '#f1fa8c'];

function renderPredictions(data) {
  const snaps = data.prediction_series || [];
  if (!snaps.length) {
    els.predMeta.textContent = 'no snapshots yet — predictions accumulate every cycle';
    els.predChart.innerHTML = '';
    return;
  }
  // mu series is the headline; bracket probs are per-bracket series.
  const muPts = snaps.map(s => ({ t: s.ts_sgt, y: s.mu }));
  const bracketNames = [];
  const seen = new Set();
  for (const s of snaps) for (const b of (s.brackets || [])) {
    if (!seen.has(b.bracket)) { seen.add(b.bracket); bracketNames.push(b.bracket); }
  }
  bracketNames.sort();

  const muColor = '#2ee8c0';
  const W = 1000, H = 280, padL = 44, padR = 16, padT = 16, padB = 26;
  const n = snaps.length;
  const xs = i => n > 1 ? padL + (i / (n - 1)) * (W - padL - padR) : padL;
  const allY = [].concat(muPts.map(p => p.y));
  for (const s of snaps) for (const b of (s.brackets || [])) allY.push(b.prob);
  const validY = allY.filter(v => v != null && isFinite(v));
  const yLo = Math.min(...validY), yHi = Math.max(...validY);
  const span = Math.max(yHi - yLo, 0.15);
  const lo = yLo - span * 0.12, hi = yHi + span * 0.12;
  const ys = v => H - padB - ((v - lo) / (hi - lo)) * (H - padT - padB);
  let grid = '';
  for (let g = 0; g <= 4; g++) {
    const v = lo + (hi - lo) * g / 4;
    grid += `<line x1="${padL}" y1="${ys(v).toFixed(1)}" x2="${W - padR}" y2="${ys(v).toFixed(1)}" stroke="var(--grid)" stroke-width="1"/>`;
    grid += `<text x="${padL - 7}" y="${(ys(v) + 3).toFixed(1)}" text-anchor="end" fill="var(--muted-ink)" font-size="9" font-family="var(--font-mono)">${v < 1 ? (v * 100).toFixed(0) + '%' : v.toFixed(1)}</text>`;
  }

  // mu line (dashed, right-side scale is °C but shares the y-axis domain
  // coarsely — mu ~31 sits above, bracket probs 0-1 sit below, so the single
  // axis reads both without a misleading dual scale).
  let muPath = '', firstMu = -1;
  muPts.forEach((p, i) => {
    if (p.y == null || !isFinite(p.y)) return;
    muPath += (firstMu < 0 ? ' M' : ' L') + xs(i).toFixed(1) + ' ' + ys(p.y).toFixed(1);
    if (firstMu < 0) firstMu = i;
  });

  // bracket prob polygons: one per bracket present in ANY snapshot.
  const bracketPaths = bracketNames.map((name, bi) => {
    let path = ''; let started = false;
    for (let i = 0; i < n; i++) {
      const b = (snaps[i].brackets || []).find(x => x.bracket === name);
      const y = b ? b.prob : null;
      if (y == null || !isFinite(y)) { started = false; continue; }
      path += (started ? ' L' : ' M') + xs(i).toFixed(1) + ' ' + ys(y).toFixed(1);
      started = true;
    }
    return { name, path, color: PRED_COLORS[bi % PRED_COLORS.length] };
  });

  const step = Math.max(1, Math.ceil(n / 8));
  let xLabels = '';
  for (let i = 0; i < n; i++) {
    if (i !== 0 && i !== n - 1 && i % step !== 0) continue;
    const lbl = snaps[i].ts_sgt.slice(11, 16);
    xLabels += `<text x="${xs(i).toFixed(1)}" y="${H - 8}" text-anchor="${i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle'}" fill="var(--muted-ink)" font-size="9" font-family="var(--font-mono)">${escapeHtml(lbl)}</text>`;
  }

  let lastMuLabel = '';
  if (firstMu >= 0) {
    const i = n - 1;
    lastMuLabel = `<text x="${xs(i).toFixed(1)}" y="${(ys(muPts[i].y) - 7).toFixed(1)}" text-anchor="end" fill="${muColor}" font-size="9" font-weight="600" font-family="var(--font-mono)">μ ${muPts[i].y.toFixed(1)}°C</text>`;
  }

  // legend chips for the brackets that appear in the latest snapshot
  const lastBrackets = (snaps[n - 1].brackets || []);
  const legendChips = bracketNames.map((name, bi) => {
    const p = lastBrackets.find(b => b.bracket === name);
    const label = name + (p ? ` ${(p.prob * 100).toFixed(0)}%` : '');
    return `<span style="display:inline-flex;align-items:center;gap:5px"><i style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${PRED_COLORS[bi % PRED_COLORS.length]}"></i>${escapeHtml(label)}</span>`;
  }).join('');
  if (els.predLegend) els.predLegend.innerHTML =
    `<span style="display:inline-flex;align-items:center;gap:5px"><i class="sw sw--mu"></i> μ (°C) — dashed</span>${legendChips}`;

  els.predMeta.textContent = n + ' cycles · ' + bracketNames.length + ' brackets';

  const hover = snaps.map((s, i) => {
    const title = s.ts_sgt + '\nμ ' + s.mu + '°C ± ' + s.sigma +
      (s.brackets || []).map(b => '\n' + b.bracket + '  ' + (b.prob * 100).toFixed(1) + '%').join('');
    const cy = s.mu != null && isFinite(s.mu) ? ys(s.mu).toFixed(1) : ys(0).toFixed(1);
    return `<rect x="${(xs(i) - 6).toFixed(1)}" y="0" width="12" height="${H}" fill="transparent" style="cursor:pointer">
      <title>${escapeHtml(title)}</title></rect>`;
  }).join('');

  els.predChart.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" style="width:100%;height:100%">
      ${grid}
      ${bracketPaths.map(bp => `<path d="${bp.path}" fill="none" stroke="${bp.color}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" opacity="0.9"/>`).join('')}
      <path d="${muPath}" fill="none" stroke="${muColor}" stroke-width="2" stroke-dasharray="5 4" stroke-linejoin="round"/>
      ${lastMuLabel}
      ${xLabels}
      <g>${hover}</g>
    </svg>`;
}

// --- latency helpers --------------------------------------------------------
function fmtLatency(ageMs) {
  if (ageMs == null) return '—';
  if (ageMs < 1000) return ageMs + ' ms ago';
  return (ageMs / 1000).toFixed(1) + ' s ago';
}
function fmtUptime(s) {
  if (s == null) return '—';
  if (s < 90) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
}
function fmtCadence(s) {
  if (s == null) return '—';
  if (s < 120) return Math.round(s) + 's / obs';
  return (s / 60).toFixed(1) + 'm / obs';
}

function renderLatency(stats) {
  if (!stats) {
    if (els.latRate) els.latRate.textContent = '—';
    return;
  }
  // merge cached METAR cadence when the live poll doesn't carry it
  if (stats.metar_cadence_s != null) _cachedCadence = stats.metar_cadence_s;

  if (els.latRate) {
    const r10 = stats.tick_rate_10s;
    els.latRate.textContent = r10 != null ? r10.toFixed(2) + ' /s' : '—';
    els.latRate.style.color = stats.connected ? 'var(--primary-ink)' : 'var(--muted-ink)';
  }
  if (els.latRateNote) {
    const r60 = stats.tick_rate_60s;
    els.latRateNote.textContent = '10s rolling · 60s: ' + (r60 != null ? r60.toFixed(2) + ' /s' : '—');
  }
  if (els.latConn) {
    const on = stats.connected;
    els.latConn.textContent = on ? '● live' : '○ offline';
    els.latConn.style.color = on ? 'var(--success)' : 'var(--muted-ink)';
  }
  if (els.latAge) {
    els.latAge.textContent = fmtLatency(stats.last_tick_age_ms);
    // flag staleness
    const stale = stats.last_tick_age_ms != null && stats.last_tick_age_ms > 3000;
    els.latAge.style.color = stale ? 'var(--warning)' : '';
  }
  if (els.latTicks) els.latTicks.textContent = (stats.ticks_total ?? 0).toLocaleString();
  if (els.latAvg) els.latAvg.textContent = (stats.avg_rate_since_start ?? 0).toFixed(2) + ' /s';
  if (els.latUp) els.latUp.textContent = fmtUptime(stats.uptime_sec);
  if (els.latMetar) {
    const c = stats.metar_cadence_s ?? _cachedCadence;
    els.latMetar.textContent = c != null ? fmtCadence(c) : '—';
  }
  if (els.latMeta) {
    const win = stats.window_s ? (stats.window_s / 3600 | 0) + 'h window' : '';
    els.latMeta.textContent = (stats.metar_points != null ? stats.metar_points + ' obs · ' : '') + win;
  }
  // push sample for the sparkline
  const r10 = stats.tick_rate_10s;
  if (r10 != null) {
    _rateHistory.push({ t: new Date(), r10, r60: stats.tick_rate_60s ?? 0 });
    if (_rateHistory.length > 180) _rateHistory.shift(); // ~9 min at 3s
  }
  renderLatencySpark();
}

function renderLatencySpark() {
  const svg = els.latSpark;
  if (!svg) return;
  const N = _rateHistory.length;
  if (N < 2) {
    svg.innerHTML = '<text x="18" y="46" fill="var(--muted-ink)" font-size="10" font-family="JetBrains Mono, monospace">accumulating rate samples…</text>';
    return;
  }
  const W = 1000, H = 90, padL = 44, padR = 16, padT = 12, padB = 22;
  const vals = _rateHistory.map(r => r.r10);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const span = Math.max(hi - lo, 0.05);
  const pad = span * 0.18;
  const loPad = lo - pad, hiPad = hi + pad;
  const xs = i => padL + (i / (N - 1 || 1)) * (W - padL - padR);
  const ys = v => H - padB - ((v - loPad) / (hiPad - loPad)) * (H - padT - padB);

  let grid = '';
  for (let g = 0; g <= 2; g++) {
    const v = loPad + (hiPad - loPad) * g / 2;
    grid += `<line x1="${padL}" y1="${ys(v).toFixed(1)}" x2="${W - padR}" y2="${ys(v).toFixed(1)}" stroke="var(--grid)" stroke-width="1"/>`;
    grid += `<text x="${padL - 7}" y="${(ys(v) + 3).toFixed(1)}" text-anchor="end" fill="var(--muted-ink)" font-size="9" font-family="var(--font-mono)">${v.toFixed(2)} /s</text>`;
  }
  let line = '';
  _rateHistory.forEach((r, i) => { line += (i ? ' L' : ' M') + xs(i).toFixed(1) + ' ' + ys(r.r10).toFixed(1); });
  line = line.trim();
  const area = line + ` L${xs(N - 1).toFixed(1)} ${ys(loPad).toFixed(1)} L${xs(0).toFixed(1)} ${ys(loPad).toFixed(1)} Z`;
  // x label: just the most-recent timestamp
  const xLab = escapeHtml(_rateHistory[N - 1].t.toLocaleTimeString('en-US', { hour12: false, timeZone: 'Asia/Singapore' }) + ' SGT');

  svg.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" style="width:100%;height:100%">
      ${grid}
      <defs><linearGradient id="latArea" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="var(--accent)" stop-opacity="0.28"/>
        <stop offset="1" stop-color="var(--accent)" stop-opacity="0.02"/>
      </linearGradient></defs>
      <path d="${area}" fill="url(#latArea)"/>
      <path d="${line}" fill="none" stroke="var(--accent)" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>
      <text x="${(W - padR).toFixed(1)}" y="${H - 4}" text-anchor="end" fill="var(--muted-ink)" font-size="9" font-family="var(--font-mono)">${xLab}</text>
    </svg>`;
}

// --- load ----------------------------------------------------------------
async function load() {
  els.metarChart.innerHTML = '';
  els.predChart.innerHTML = '';
  try {
    const res = await fetch('/api/analytics');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    renderMetar(data);
    renderPredictions(data);
    if (data.feed_stats) renderLatency(data.feed_stats);
    // refresh every 30s to keep up with new METAR obs + model cycles
    setTimeout(load, 30000);
  } catch (e) {
    els.metarMeta.textContent = 'failed: ' + e.message + ' — retrying in 30s';
    els.predMeta.textContent = 'failed: ' + e.message + ' — retrying in 30s';
    setTimeout(load, 30000);
  }
}

async function tickLatency() {
  // lightweight 3s poll so the points/sec readout feels live
  try {
    const res = await fetch('/api/feed_stats');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const d = await res.json();
    if (d.feed_stats) renderLatency(d.feed_stats);
  } catch (_) { /* leave the last value on screen */ }
}

load();
setTimeout(tickLatency, 900);
setInterval(tickLatency, 3000);