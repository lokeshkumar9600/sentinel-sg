/* Sentinel — Simulations page: test the model against hypothetical conditions */
const $ = (id) => document.getElementById(id);

const els = {
  clock: $('clock'),
  run: $('sim-run'),
  hint: $('sim-hint'),
  eventDate: $('sim-event-date'),
  mu: $('sim-mu'),
  sigma: $('sim-sigma'),
  chart: $('sim-chart'),
  context: $('sim-context'),
  mc: $('sim-mc'),
  bracketsBody: $('sim-brackets-body'),
};

// Range controls -> <span> value readouts
const CTLS = [
  ['hour', 'ctl-hour', 'ctl-hour-val', v => `${v}:00 SGT`],
  ['temp', 'ctl-temp', 'ctl-temp-val', v => `${Number(v).toFixed(1)} °C`],
  ['rh', 'ctl-rh', 'ctl-rh-val', v => `${v} %`],
  ['wind', 'ctl-wind', 'ctl-wind-val', v => `${v} kt`],
  ['dewp', 'ctl-dewp', 'ctl-dewp-val', v => `${Number(v).toFixed(1)} °C`],
  ['cloud', 'ctl-cloud', 'ctl-cloud-val', v => `${v}/8`],
];
CTLS.forEach(([key, inputId, valId, fmt]) => {
  const input = $(inputId), out = $(valId);
  if (!input) return;
  const sync = () => { out.textContent = fmt(input.value); };
  input.addEventListener('input', sync);
  sync();
});

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

function scenario() {
  return {
    hour: Number($('ctl-hour').value),
    temp: Number($('ctl-temp').value),
    rh: Number($('ctl-rh').value),
    wind: Number($('ctl-wind').value),
    dewp: Number($('ctl-dewp').value),
    cloud: Number($('ctl-cloud').value),
    storm: $('ctl-storm').checked,
  };
}

// --- Prediction chart (SVG normal curve, mirrors app.js geometry) ---
function renderChart(prediction) {
  const mu = prediction.mean_c, sigma = Math.max(0.15, prediction.std_c);
  const W = 300, H = 68, padL = 34, padR = 12, padT = 10, padB = 16;
  const lo = mu - 3.4 * sigma, hi = mu + 3.4 * sigma;
  const xs = t => padL + ((t - lo) / (hi - lo)) * (W - padL - padR);
  const ymax = 1 / (sigma * Math.sqrt(2 * Math.PI));
  const ys = y => H - padB - (y / ymax) * (H - padT - padB);
  const pdf = x => Math.exp(-0.5 * ((x - mu) / sigma) ** 2) / (sigma * Math.sqrt(2 * Math.PI));

  let line = '';
  for (let i = 0, n = 60; i <= n; i++) {
    const x = lo + (i / n) * (hi - lo);
    line += (i ? ' L' : 'M') + xs(x).toFixed(1) + ' ' + ys(pdf(x)).toFixed(1);
  }
  const area = line + ` L${xs(hi).toFixed(1)} ${ys(0).toFixed(1)} L${xs(lo).toFixed(1)} ${ys(0).toFixed(1)} Z`;

  els.chart.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" style="width:100%;height:100%">
      <path d="${area}" fill="var(--accent)" opacity="0.16"/>
      <path d="${line}" fill="none" stroke="var(--accent)" stroke-width="1.6"/>
      <line x1="${xs(mu).toFixed(1)}" y1="${padT}" x2="${xs(mu).toFixed(1)}" y2="${H - padB - 2}"
        stroke="var(--accent)" stroke-width="1" stroke-dasharray="2 3"/>
      <text x="${xs(mu).toFixed(1)}" y="${padT - 2}" fill="var(--primary-ink)" font-size="9"
        text-anchor="middle" font-family="var(--font-mono)">${mu.toFixed(1)}°C</text>
    </svg>`;
}

function renderContext(context) {
  els.context.innerHTML = (context || []).map(c =>
    `<span class="ctx-chip">${escapeHtml(c)}</span>`).join('')
    || '<span class="text--muted" style="font-size:0.75rem;color:var(--muted-ink)">No context factors — trivial scenario.</span>';
}

// --- Monte Carlo panel: summary + histogram + per-bracket odds ---
function renderMc(mc, trades) {
  if (!mc) { els.mc.innerHTML = '<span class="text--muted">Awaiting simulation...</span>'; return; }
  const maxC = Math.max(1, ...mc.histogram.map(b => b.count));
  const bars = mc.histogram.map(b => {
    const h = Math.max(3, Math.round((b.count / maxC) * 100));
    return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;min-width:0">
      <div style="width:70%;height:${h}%;background:var(--accent);opacity:0.85;border-radius:2px 2px 0 0"></div>
      <span style="font-size:7px;color:var(--muted-ink);writing-mode:vertical-rl;transform:rotate(180deg);overflow:hidden;max-height:20px;white-space:nowrap">${b.lo.toFixed(1)}</span>
    </div>`;
  }).join('');

  const mcRows = (trades.filter(t => t.mc_win_rate != null)).map(t => {
    const exp = t.mc_exp_pnl;
    const cls = exp != null ? (exp >= 0 ? 'green' : 'red') : '';
    return `<div style="display:flex;justify-content:space-between;gap:10px;padding:4px 0;border-bottom:1px solid var(--hairline)">
      <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(t.bracket || '')}</span>
      <span style="flex:0 0 96px;text-align:right">win <b>${(t.mc_win_rate * 100).toFixed(0)}%</b></span>
      <span style="flex:0 0 110px;text-align:right;color:var(--secondary-ink)">${t.mc_low != null ? '[' + t.mc_low.toFixed(1) + '–' + t.mc_high.toFixed(1) + ']' : ''}</span>
      <span style="flex:0 0 88px;text-align:right" class="${cls}">E[PnL] ${exp != null ? (exp >= 0 ? '+' : '') + '$' + exp.toFixed(2) : '—'}</span>
    </div>`;
  }).join('') || '<div class="emptystate">No tradable brackets.</div>';

  els.mc.innerHTML = `
    <div style="color:var(--secondary-ink);font-size:0.76rem;display:flex;flex-wrap:wrap;gap:14px;margin-bottom:10px">
      <span>mean <b style="color:var(--primary-ink)">${mc.mean_c.toFixed(2)}°C</b></span>
      <span>σ <b style="color:var(--primary-ink)">${mc.std_c.toFixed(2)}</b></span>
      <span>p10–p90 <b style="color:var(--primary-ink)">${mc.p10.toFixed(1)} – ${mc.p90.toFixed(1)}°C</b></span>
    </div>
    <div style="height:56px;display:flex;gap:2px;align-items:stretch;margin-bottom:6px">${bars}</div>
    <div style="margin-top:8px">${mcRows}</div>`;
}

// --- Decision table ---
function tag(action) {
  const cls = (action || 'NO_TRADE').toUpperCase();
  return `<span class="tag tag--${escapeHtml(cls)}">${escapeHtml(cls)}</span>`;
}

function renderBrackets(data) {
  const trades = data.trades || [];
  els.eventDate.textContent = data.event_date_str || '—';
  if (data.error) {
    els.bracketsBody.innerHTML = `<tr><td colspan="7" class="emptystate">${escapeHtml(data.error)}</td></tr>`;
    return;
  }
  if (!trades.length) {
    els.bracketsBody.innerHTML = '<tr><td colspan="7" class="emptystate">No open brackets on the live market.</td></tr>';
    return;
  }
  els.bracketsBody.innerHTML = trades.map(t => {
    const prob = t.prob != null ? (t.prob * 100).toFixed(1) + '%' : '—';
    const stake = t.stake_usd != null ? '$' + Number(t.stake_usd).toFixed(2) : '—';
    return `<tr>
      <td>${escapeHtml(t.bracket || '')}</td>
      <td class="num">${prob}</td>
      <td class="num yes">${t.yes_price != null ? '¢' + (t.yes_price * 100).toFixed(1) : '—'}</td>
      <td class="num no">${t.no_price != null ? '¢' + (t.no_price * 100).toFixed(1) : '—'}</td>
      <td>${tag(t.action)}</td>
      <td class="num">${t.edge != null ? (t.edge * 100).toFixed(1) + '%' : '—'}</td>
      <td class="num">${stake}</td>
    </tr>`;
  }).join('');
}

// --- Run ---
async function runSim() {
  const s = scenario();
  els.run.disabled = true;
  els.hint.textContent = 'Running model + Monte Carlo...';
  els.mc.innerHTML = '<span class="text--muted">Running 5,000 Monte Carlo draws...</span>';

  try {
    const res = await fetch('/api/simulate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(s),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const p = data.prediction || {};

    if (p.mean_c != null) {
      els.mu.textContent = p.mean_c.toFixed(1) + '°C';
      els.sigma.textContent = `±${p.std_c != null ? p.std_c.toFixed(2) : '·'}°C · storm ${p.storm_score != null ? p.storm_score.toFixed(2) : '·'}`;
    } else {
      els.mu.textContent = '—';
      els.sigma.textContent = 'no prediction';
    }

    // Only draw the chart if the prediction is meaningful (finite valid range).
    if (p.mean_c != null && p.std_c != null && p.std_c > 0.01 && isFinite(p.mean_c)) {
      renderChart(p);
    } else {
      els.chart.innerHTML = '';
    }

    renderContext(data.context);
    renderMc(data.mc, data.trades);
    renderBrackets(data);

    els.hint.textContent = 'Done. Adjust conditions and run again to compare scenarios.';
  } catch (e) {
    els.hint.textContent = 'Failed: ' + e.message;
    els.mc.innerHTML = `<div class="emptystate">Simulation error: ${escapeHtml(e.message)}</div>`;
  } finally {
    els.run.disabled = false;
  }
}

els.run.addEventListener('click', runSim);