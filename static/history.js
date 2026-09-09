/* Sentinel — History & Performance page */
const POLL = 20;
const $ = (id) => document.getElementById(id);

const els = {
  clock: $('clock'),
  perfDays: $('ps-days'),
  perfMae: $('ps-mae'),
  perfBias: $('ps-bias'),
  perfHit: $('ps-hit'),
  perfLockon: $('ps-lockon'),
  perfHeld: $('ps-held'),
  epTime: $('ep-time'),
  epBody: $('ep-body'),
  body: $('history-body'),
  filters: $('history-filters'),
};

// --- Clock ---
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

// --- Model performance tiles ---
async function loadPerformance() {
  try {
    const d = await (await fetch('/api/performance')).json();
    els.perfDays.textContent = d.days_tracked != null ? d.days_tracked : '—';

    if (d.mae != null) {
      els.perfMae.textContent = d.mae.toFixed(2) + '°C';
    } else {
      els.perfMae.textContent = '—';
    }

    if (d.bias != null) {
      const sign = d.bias > 0 ? '+' : '';
      els.perfBias.textContent = sign + d.bias.toFixed(2) + '°C';
      els.perfBias.style.color = Math.abs(d.bias) < 0.2 ? 'var(--secondary-ink)'
        : d.bias > 0 ? 'var(--warning)' : 'var(--danger)';
    } else {
      els.perfBias.textContent = '—';
    }

    if (d.hit_rate_1sigma != null) {
      els.perfHit.textContent = (d.hit_rate_1sigma * 100).toFixed(0) + '%';
      els.perfHit.style.color = d.hit_rate_1sigma >= 0.68 ? 'var(--success)'
        : d.hit_rate_1sigma >= 0.5 ? 'var(--warning)' : 'var(--danger)';
    } else {
      els.perfHit.textContent = '—';
    }
  } catch (e) {
    els.perfMae.textContent = els.perfBias.textContent = els.perfHit.textContent = '—';
  }
}

// --- Early-prediction analysis (lock-on speed) ---
function fmtDay(dateStr) {
  if (!dateStr) return '—';
  const m = String(dateStr).match(/([A-Za-z]+)-(\d+)-(\d{4})/);
  if (!m) return escapeHtml(dateStr);
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const mi = months.indexOf(m[1].slice(0, 3));
  return (mi >= 0 ? months[mi] : m[1].slice(0, 3)) + ' ' + m[2] + ', ' + m[3];
}

function fmtHour(h) {
  // 14 -> "14:00 SGT"
  if (h == null || !isFinite(h)) return '—';
  return String(Math.round(h)).padStart(2, '0') + ':00 SGT';
}

async function loadEarlyPrediction() {
  try {
    const d = await (await fetch('/api/early_prediction')).json();
    const summary = d.summary || {};
    const days = d.days || [];

    // Summary tiles
    els.perfDays.textContent = summary.days_analyzed != null ? summary.days_analyzed : '—';
    els.perfLockon.textContent = summary.avg_lock_on_hour != null
      ? fmtHour(summary.avg_lock_on_hour).replace(':00 SGT', '') + 'h'
      : '—';
    els.perfHeld.textContent = summary.pct_held_after_lock_on != null
      ? (summary.pct_held_after_lock_on * 100).toFixed(0) + '%'
      : '—';
    els.epTime.textContent = summary.days_analyzed != null
      ? summary.days_analyzed + (summary.days_analyzed === 1 ? ' day analyzed' : ' days analyzed')
      : '—';

    if (!days.length) {
      els.epBody.innerHTML = '<tr><td colspan="7" class="emptystate">No settled days with timeseries snapshots yet — lock-on analysis grows each day as the analytics store accrues minute-by-minute predictions.</td></tr>';
      return;
    }

    els.epBody.innerHTML = days.map(r => {
      const lockHour = r.lock_on_hour != null ? fmtHour(r.lock_on_hour) : '<span class="text--muted">never</span>';
      const held = r.held == null
        ? '<span class="text--muted">—</span>'
        : r.held
          ? '<span style="color:var(--success)">yes</span>'
          : '<span style="color:var(--danger)">no — wobbled</span>';
      const pLock = r.confidence_at_lock_in != null
        ? (r.confidence_at_lock_in * 100).toFixed(1) + '%'
        : '—';
      const pEnd = r.winner_prob_end != null
        ? (r.winner_prob_end * 100).toFixed(1) + '%'
        : '—';
      const note = r.note ? ` <span class="text--muted" style="font-size:0.66rem">(${escapeHtml(r.note)})</span>` : '';
      return `<tr>
        <td>${fmtDay(r.date)}${note}</td>
        <td>${r.actual_max != null ? r.actual_max.toFixed(1) + '°C' : '<span class="text--muted">—</span>'}</td>
        <td>${escapeHtml(r.winner_bracket || '—')}</td>
        <td>${lockHour}</td>
        <td>${held}</td>
        <td class="num">${pLock}</td>
        <td class="num">${pEnd}</td>
      </tr>`;
    }).join('');
  } catch (e) {
    els.perfLockon.textContent = '—';
    els.perfHeld.textContent = '—';
    els.epBody.innerHTML = `<tr><td colspan="7" class="emptystate">Early-prediction analysis unavailable: ${escapeHtml(e.message)}</td></tr>`;
  }
}

// --- Signal log with client-side filtering ---
let _history = [];
let _filter = 'ALL';

function signalMatches(sig) {
  const s = (sig || '').toUpperCase();
  if (_filter === 'TRADED') return s.startsWith('ENTER_');
  if (_filter === 'HELD') return s.startsWith('HOLD_') || s === 'TAKE_PROFIT' || s === 'STOP';
  if (_filter === 'SKIPPED') return s === 'SKIP' || s === 'TIMING_HOLD' || s === 'NO_TRADE';
  return true;
}

async function loadHistory() {
  try {
    const d = await (await fetch('/api/history?limit=500')).json();
    _history = d.history || [];
  } catch (e) {
    _history = [];
  }
  renderHistory();
}

function renderHistory() {
  const rows = _history.filter(h => signalMatches(h.signal));
  if (!rows.length) {
    els.body.innerHTML = '<tr><td colspan="9" class="emptystate">No signals yet — the model has not run on this deployment.</td></tr>';
    return;
  }
  const fmtPrice = p => p != null && isFinite(p) ? '$' + Number(p).toFixed(2) : '—';
  const fmtPct = v => v != null && isFinite(v) ? (v * 100).toFixed(1) + '%' : '—';

  els.body.innerHTML = rows.map(h => {
    const sig = (h.signal || 'NO_TRADE').toUpperCase();
    const pnl = fmtPct(h.pnl_pct);
    const pnlCls = h.pnl_pct == null ? '' : h.pnl_pct >= 0 ? 'green' : 'red';
    const stake = h.stake_usd ? '$' + Number(h.stake_usd).toFixed(2) : '—';
    const edge = h.edge ? fmtPct(h.edge) : '—';
    return `<tr>
      <td>${escapeHtml(h.timestamp_sgt || '')}</td>
      <td><span class="tag tag--${escapeHtml(sig)}">${escapeHtml(sig)}</span></td>
      <td>${escapeHtml(h.bracket || '—')}</td>
      <td>${escapeHtml(h.side || '—')}</td>
      <td class="num">${fmtPrice(h.entry_price)}</td>
      <td class="num">${fmtPrice(h.exit_price)}</td>
      <td class="num ${pnlCls}">${pnl}</td>
      <td class="num">${stake}</td>
      <td>${escapeHtml(h.reason || '')}</td>
    </tr>`;
  }).join('');
}

// Filters
els.filters.addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-filter]');
  if (!btn) return;
  _filter = btn.dataset.filter;
  els.filters.querySelectorAll('button').forEach(b => {
    b.dataset.active = b === btn ? 'true' : 'false';
  });
  renderHistory();
});

// --- Boot ---
loadPerformance();
loadEarlyPrediction();
loadHistory();
setInterval(loadHistory, POLL * 1000);
setInterval(loadPerformance, POLL * 2000);
setInterval(loadEarlyPrediction, POLL * 2000);