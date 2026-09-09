/* Sentinel — Backtest page: replay the settled journal as a bracket strategy. */
const $ = (id) => document.getElementById(id);

const els = {
  clock: $('clock'),
  assumption: $('bt-assumption'),
  ret: $('bt-return'),
  pnl: $('bt-pnl'),
  winrate: $('bt-winrate'),
  wl: $('bt-wl'),
  pf: $('bt-pf'),
  trades: $('bt-trades'),
  curveTime: $('bt-curve-time'),
  curve: $('bt-curve'),
  body: $('bt-body'),
  dayBody: $('bt-day-body'),
};

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

function money(v) {
  if (v == null) return '—';
  return (v >= 0 ? '+' : '') + '$' + Number(v).toFixed(2);
}

function renderCurve(curve) {
  const el = els.curve;
  if (!curve || curve.length < 1) {
    el.innerHTML = '<text x="10" y="20" fill="var(--muted-ink)" font-size="10">No settled days yet — past days settle automatically once the WSSS max is known.</text>';
    return;
  }
  const W = 1000, H = 210, padL = 56, padR = 24, padT = 18, padB = 30;
  const vals = curve.map(c => c.equity);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const span = Math.max(hi - lo, 1);
  const loPad = lo - span * 0.15, hiPad = hi + span * 0.15;
  const xs = i => padL + (i / (curve.length - 1 || 1)) * (W - padL - padR);
  const ys = v => H - padB - ((v - loPad) / (hiPad - loPad)) * (H - padT - padB);

  // baseline gridlines
  const gridLines = [];
  for (let g = 0; g <= 4; g++) {
    const v = loPad + (hiPad - loPad) * g / 4;
    gridLines.push(`<line x1="${padL}" y1="${ys(v).toFixed(1)}" x2="${W - padR}" y2="${ys(v).toFixed(1)}" stroke="var(--grid)" stroke-width="1"/>`);
    gridLines.push(`<text x="${padL - 8}" y="${(ys(v) + 3).toFixed(1)}" text-anchor="end" fill="var(--muted-ink)" font-size="9" font-family="var(--font-mono)">$${v.toFixed(0)}</text>`);
  }

  let line = curve.map((c, i) => (i ? 'L' : 'M') + xs(i).toFixed(1) + ' ' + ys(c.equity).toFixed(1)).join(' ');
  const area = line + ` L${xs(curve.length - 1).toFixed(1)} ${ys(loPad).toFixed(1)} L${xs(0).toFixed(1)} ${ys(loPad).toFixed(1)} Z`;

  const up = curve[curve.length - 1].equity >= curve[0].equity;
  const col = up ? '#2ee8c0' : '#ff6b8d';

  const xLabels = curve
    .filter((_, i) => i === 0 || i === curve.length - 1 || i % Math.max(1, Math.ceil(curve.length / 8)) === 0)
    .map(c => `<text x="${xs(curve.indexOf(c)).toFixed(1)}" y="${H - 8}" text-anchor="middle" fill="var(--muted-ink)" font-size="9" font-family="var(--font-mono)">${escapeHtml(fmtShortDate(c.date))}</text>`)
    .join('');

  // start/end equity labels
  const endLabel = `<g>
    <circle cx="${xs(curve.length - 1).toFixed(1)}" cy="${ys(curve[curve.length - 1].equity).toFixed(1)}" r="4" fill="${col}"/>
    <text x="${xs(curve.length - 1).toFixed(1)}" y="${(ys(curve[curve.length - 1].equity) - 10).toFixed(1)}" text-anchor="middle" fill="var(--primary-ink)" font-size="10" font-weight="600" font-family="var(--font-mono)">$${curve[curve.length - 1].equity.toFixed(2)}</text>
  </g>`;

  el.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
      ${gridLines.join('')}
      <defs><linearGradient id="btArea" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="${col}" stop-opacity="0.28"/><stop offset="1" stop-color="${col}" stop-opacity="0.02"/>
      </linearGradient></defs>
      <path d="${area}" fill="url(#btArea)"/>
      <path d="${line}" fill="none" stroke="${col}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      ${xLabels}${endLabel}
    </svg>`;
}

function tag(action) {
  const cls = String(action || 'NO_TRADE').toUpperCase();
  return `<span class="tag tag--${escapeHtml(cls)}">${escapeHtml(action || '—')}</span>`;
}

function timeOnly(ts) {
  if (!ts) return '—';
  const m = String(ts).match(/(\d{2}:\d{2}:\d{2})/);
  return m ? m[1] : ts;
}

function fmtDay(dateStr) {
  // "September-05-2026" -> "05 Sep 2026"
  if (!dateStr) return '—';
  const m = String(dateStr).match(/([A-Za-z]+)-(\d+)-(\d{4})/);
  if (!m) return escapeHtml(dateStr);
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const mi = months.indexOf(m[1].slice(0, 3));
  return (mi >= 0 ? months[mi] : m[1].slice(0, 3)) + ' ' + m[2] + ', ' + m[3];
}

const MONTH_NAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

function fmtShortDate(dateStr) {
  // "09-05" -> "Sep 5", or "September-05-2026" -> "Sep 5"
  if (!dateStr) return '';
  // Try MM-DD first
  const md = String(dateStr).match(/^(\d{2})-(\d{2})/);
  if (md) return MONTH_NAMES[parseInt(md[1], 10) - 1] + ' ' + parseInt(md[2], 10);
  // Try "Month-DD-YYYY"
  const full = String(dateStr).match(/([A-Za-z]+)-(\d+)-(\d{4})/);
  if (full) {
    const mi = MONTH_NAMES.indexOf(full[1].slice(0, 3));
    return (mi >= 0 ? MONTH_NAMES[mi] : full[1].slice(0, 3)) + ' ' + parseInt(full[2], 10);
  }
  return dateStr;
}

function renderPerDay(rows) {
  if (!rows || !rows.length) {
    els.dayBody.innerHTML = '<tr><td colspan="6" class="emptystate">No journal entries yet — the model records each day once it runs.</td></tr>';
    return;
  }
  els.dayBody.innerHTML = rows.map(r => {
    const mu = r.predicted_mu, sigma = r.predicted_sigma, actual = r.actual_max;
    const pred = mu != null && isFinite(mu)
      ? mu.toFixed(1) + ' ± ' + (sigma != null && isFinite(sigma) ? sigma.toFixed(2) : '—')
      : '—';
    const actualStr = actual != null && isFinite(actual) ? actual.toFixed(1) + '°C' : '<span class="text--muted">unsettled</span>';
    const err = r.error != null && isFinite(r.error)
      ? (r.error >= 0 ? '+' : '') + r.error.toFixed(1) + '°'
      : '—';
    const errCls = r.error == null ? '' : Math.abs(r.error) <= (sigma || 1) ? 'green' : 'red';
    const pb = r.p_bracket != null && isFinite(r.p_bracket)
      ? (r.p_bracket * 100).toFixed(1) + '%'
      : '<span class="text--muted">—</span>';
    const ts = r.trade_status;
    const tradeTag = ts === 'traded'
      ? '<span class="tag tag--ENTER_YES" style="font-size:0.62rem">TRADED</span>'
      : ts === 'no_trade'
        ? '<span class="tag tag--SKIP" style="font-size:0.62rem">SKIPPED</span>'
        : '<span class="text--muted">—</span>';
    return `<tr>
      <td>${fmtDay(r.date)}${r.hour_of_day != null ? ' <span class="text--muted" style="font-size:0.66rem">' + escapeHtml(r.hour_of_day) + ':00</span>' : ''}</td>
      <td class="num">${pred}</td>
      <td class="num">${actualStr}</td>
      <td class="num ${errCls}">${err}</td>
      <td class="num">${pb}</td>
      <td style="text-align:center">${tradeTag}</td>
    </tr>`;
  }).join('');
}

function renderTrades(trades) {
  if (!trades || !trades.length) {
    els.body.innerHTML = '<tr><td colspan="8" class="emptystate">No executed trades yet — entries and their stops/take-profits appear here as the live book settles them.</td></tr>';
    return;
  }
  els.body.innerHTML = trades.map(t => {
    const pnlCls = t.pnl != null ? (t.pnl > 0 ? 'green' : t.pnl < 0 ? 'red' : '') : '';
    const open = t.action === 'OPEN';
    const sig = open
      ? `<span class="tag tag--OPEN">OPEN</span>`
      : `<span class="tag tag--${escapeHtml(t.exit_signal || t.action)}">${escapeHtml(t.exit_signal || t.action)}</span>`;
    const side = t.side ? ` (${escapeHtml(t.side)})` : '';
    return `<tr>
      <td style="white-space:nowrap">
        <span style="color:var(--secondary-ink)">${escapeHtml(timeOnly(t.entry_at))}</span>
        → <span style="color:var(--secondary-ink)">${escapeHtml(timeOnly(t.exit_at))}</span>
        <span class="text--muted" style="display:block;font-size:0.66rem;color:var(--muted-ink)">${escapeHtml((t.entry_at || '').slice(0, 10))}</span>
      </td>
      <td>${escapeHtml(t.bracket)}${side}</td>
      <td class="num">${t.entry_price != null ? '¢' + (t.entry_price * 100).toFixed(1) : '—'}</td>
      <td class="num">${t.exit_price != null ? '¢' + (t.exit_price * 100).toFixed(1) : '—'}</td>
      <td class="num">${t.edge != null ? (t.edge * 100).toFixed(2) + '%' : '—'}</td>
      <td>${sig}</td>
      <td class="num ${pnlCls}">${t.pnl != null ? money(t.pnl) : '<span class="text--muted">open</span>'}</td>
      <td class="num">${t.equity != null ? Number(t.equity).toFixed(2) : '—'}</td>
    </tr>`;
  }).join('');
}

async function load() {
  els.body.innerHTML = '<tr><td colspan="9" class="emptystate">Loading backtest...</td></tr>';
  try {
    const res = await fetch('/api/backtest');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const d = await res.json();

    els.assumption.textContent = ' · ' + escapeHtml(d.assumption || '');

    const ret = d.return_pct;
    const pnl = d.total_pnl;
    els.ret.textContent = ret != null ? (ret >= 0 ? '+' : '') + ret.toFixed(2) + '%' : '—';
    els.ret.style.color = ret != null ? (ret >= 0 ? 'var(--success)' : 'var(--danger)') : '';
    els.pnl.textContent = money(pnl);
    els.pnl.style.color = pnl != null ? (pnl >= 0 ? 'var(--success)' : 'var(--danger)') : '';
    els.winrate.textContent = d.win_rate != null ? (d.win_rate * 100).toFixed(1) + '%' : '—';
    els.wl.textContent = d.wins != null ? d.wins + ' wins / ' + d.losses + ' losses' : '';
    els.pf.textContent = d.profit_factor != null ? d.profit_factor.toFixed(3) : '—';
    els.trades.textContent = d.trades_taken != null
      ? d.trades_taken + ' closed · ' + (d.open_positions != null ? d.open_positions : 0) + ' open' : '';

    els.curveTime.textContent = d.days_settled != null ? d.days_settled + ' settled days' : '';
    renderCurve(d.curve);
    renderTrades(d.trades);
    renderPerDay(d.per_day);
  } catch (e) {
    els.body.innerHTML = `<tr><td colspan="9" class="emptystate">Backtest error: ${escapeHtml(e.message)}</td></tr>`;
  }
}

load();