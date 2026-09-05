/* Sentinel — History & Performance page */
const POLL = 20;
const $ = (id) => document.getElementById(id);

const els = {
  clock: $('clock'),
  perfDays: $('ps-days'),
  perfMae: $('ps-mae'),
  perfBias: $('ps-bias'),
  perfHit: $('ps-hit'),
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
loadHistory();
setInterval(loadHistory, POLL * 1000);
setInterval(loadPerformance, POLL * 2000);