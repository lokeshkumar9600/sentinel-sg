/* Sentinel Prediction Engine — Palantir-Style Digital Twin */

// Polling intervals
const POLL_FULL = 30;        // dashboard: 30s
const POLL_WSSS = 15;        // WSSS: 15s
const POLL_SPATIAL = 8;      // map: 8s
const POLL_PRICES = 0.1;     // prices: 0.1s

const $ = (id) => document.getElementById(id);

// --- State ---
let _map = null;
let _mapLayer = null;
let _currentMetric = 'air_temperature';
let _priceSeq = 0;
let _priceTicks = 0;
let _lastMoveAt = null;
let _prevYes = [], _prevNo = [];
let _lastModelProbs = [];

// --- DOM Elements ---
const els = {
  clock: $('clock'),
  systemStatus: $('system-status'),
  refresh: $('refresh-btn'),
  predValue: $('pred-value'),
  predMeta: $('pred-meta'),
  predChart: $('pred-chart'),
  contextList: $('context-list'),
  feedStatus: $('feed-status'),
  tickCount: $('tick-count'),
  lastMove: $('last-move'),
  flowSvg: $('flow-svg'),
  wsssTime: $('wsss-time'),
  wsssTemp: $('wsss-temp'),
  wsssFlight: $('wsss-flight'),
  wsssWx: $('wsss-wx'),
  wsssDewp: $('wsss-dewp'),
  wsssRh: $('wsss-rh'),
  wsssWind: $('wsss-wind'),
  wsssGust: $('wsss-gust'),
  wsssPres: $('wsss-pres'),
  wsssTrend: $('wsss-trend'),
  wsssVis: $('wsss-vis'),
  wsssCloud: $('wsss-cloud'),
  wsssCeil: $('wsss-ceil'),
  wsssSparkline: $('wsss-sparkline'),
  decisionFlow: $('decision-flow'),
  metricToggle: $('metric-toggle'),
  mapLegend: $('map-legend'),
  mapTimestamp: $('map-timestamp'),
  positionsBody: $('positions-body'),
  bracketsBody: $('brackets-body'),
  bracketsFoot: $('brackets-foot'),
  eventDate: $('event-date'),
};

// --- Clock & System ---
function tickClock() {
  const now = new Date();
  const sgt = new Date(now.toLocaleString('en-US', { timeZone: 'Asia/Singapore' }));
  els.clock.textContent = sgt.toLocaleTimeString('en-US', { hour12: false }) + ' SGT';
}

function setSystemStatus(state, text) {
  els.systemStatus.textContent = text;
  els.systemStatus.dataset.state = state;
}

// --- Data Flow Pipeline (cinematic) ---
let _flowPrev = {};

function flowIcon(label, color) {
  const c = color;
  switch (label) {
    case 'INGEST': return `<g fill="none" stroke="${c}" stroke-width="1.6" stroke-linecap="round">
      <ellipse cx="0" cy="-8" rx="7" ry="3"/><path d="M-7 -8v5a7 3 0 0 0 14 0v-5"/><path d="M-7 -3v5a7 3 0 0 0 14 0v-5"/>
    </g>`;
    case 'FEATURES': return `<g fill="${c}">
      ${[[-5,-5],[5,-5],[-5,5],[5,5],[0,0],[-8,0],[8,0],[0,-8],[0,8]].map(p => `<circle cx="${p[0]}" cy="${p[1]}" r="1.1"/>`).join('')}
    </g>`;
    case 'MODEL': return `<g fill="none" stroke="${c}" stroke-width="1.6">
      <circle r="8"/><circle r="4" stroke-width="1.2"/><circle r="1" fill="${c}" stroke="none"/>
    </g>`;
    case 'STRESS': return `<path d="M1 -10 L-5 0 L0 0 L-1 10 L5 -1 L0 -1 Z" fill="${c}" stroke="none"/>`;
    case 'DECISION': return `<g fill="none" stroke="${c}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
      <path d="M-8 0h12M-2 -6l6 6-6 6"/>
    </g>`;
    default: return `<circle r="6" fill="${c}" stroke="none"/>`;
  }
}

function renderDataFlow(data) {
  const W = 1000, H = 250, MID = 90, R = 38;
  const f = data.features || {};
  const pred = data.prediction || {};
  const trades = (data.event && data.event.trades) || [];
  const top = trades.find(t => t.edge != null) || null;
  const nFeat = Object.keys(f).length;

  const stages = [
    { label: 'INGEST', color: '#38bdf8',
      value: f.wsss_current_temp != null ? f.wsss_current_temp.toFixed(1) + '°C' : '···',
      sub: 'wsss · gov.sg', on: f.wsss_current_temp != null },
    { label: 'FEATURES', color: '#a78bfa',
      value: f.wsss_rh != null ? f.wsss_rh.toFixed(0) + '% RH' : '···',
      sub: nFeat + ' signals', on: nFeat > 0 },
    { label: 'MODEL', color: '#4285f4',
      value: pred.mean_c != null ? (pred.mean_c.toFixed(1) + '±' + (pred.std_c != null ? pred.std_c.toFixed(1) : '·')) : '···',
      sub: 'max-temp dist', on: pred.mean_c != null },
    { label: 'STRESS', color: '#f59e0b',
      value: f.rain_dist_to_changi_km != null ? f.rain_dist_to_changi_km.toFixed(1) + 'km' : 'CLEAR',
      sub: (f.changi_forecast_storm ? 'storm active' : 'convection ok'),
      on: f.changi_forecast_storm || f.rain_dist_to_changi_km != null },
    { label: 'DECISION', color: '#34d399',
      value: top ? (top.edge * 100).toFixed(1) + '% edge' : '—',
      sub: top ? (top.action || 'signal') : 'no edge',
      on: !!top },
  ];

  const n = stages.length;
  const xs = [];
  for (let i = 0; i < n; i++) xs[i] = (W - 180) * (i / (n - 1)) + 90;

  let svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">`;
  svg += `<defs>
    <linearGradient id="flowLine" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#38bdf8"/><stop offset="0.5" stop-color="#a78bfa"/>
      <stop offset="1" stop-color="#34d399"/>
    </linearGradient>
    <filter id="flowGlow" x="-80%" y="-80%" width="260%" height="260%">
      <feGaussianBlur stdDeviation="4" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
  </defs>`;

  // Connectors (S-curve) with energy streams + traveling particles
  stages.forEach((s, i) => {
    if (i === n - 1) return;
    const x1 = xs[i] + R - 4, x2 = xs[i + 1] - R + 4;
    const midX = (x1 + x2) / 2;
    const d = `M${x1},${MID} C${midX},${MID - 46} ${midX},${MID + 46} ${x2},${MID}`;
    const pid = 'fp' + i;
    svg += `<path id="${pid}" d="${d}" fill="none" stroke="rgba(120,140,180,0.10)" stroke-width="2"/>`;
    svg += `<path d="${d}" fill="none" stroke="url(#flowLine)" stroke-width="2" stroke-linecap="round"
      stroke-dasharray="1 14" stroke-dashoffset="6" opacity="0.9">
      <animate attributeName="stroke-dashoffset" from="0" to="-30" dur="${1.1 - i * 0.15}s" repeatCount="indefinite"/>
    </path>`;
    for (let p = 0; p < 3; p++) {
      svg += `<circle r="${p === 1 ? 2.4 : 1.6}" fill="${stages[i + 1].color}" filter="url(#flowGlow)">
        <animateMotion dur="${2.2 + i * 0.3}s" repeatCount="indefinite" begin="${p * 0.7}s">
          <mpath href="#${pid}"/>
        </animateMotion>
      </circle>`;
      svg += `<circle r="1" fill="#fff" opacity="0.8">
        <animateMotion dur="${2.2 + i * 0.3}s" repeatCount="indefinite" begin="${p * 0.7}s">
          <mpath href="#${pid}"/>
        </animateMotion>
      </circle>`;
    }
  });

  // Stage nodes
  stages.forEach((s, i) => {
    const x = xs[i];
    const flash = _flowPrev[s.label] !== s.value;
    if (flash) _flowPrev[s.label] = s.value;

    svg += `<g transform="translate(${x},${MID})" class="flow-stage">
      <g class="${flash ? 'stage-flash' : ''}">`;
    // Breathing halo
    svg += `<circle r="${R + 16}" fill="${s.color}" opacity="${s.on ? 0.10 : 0.03}">
      <animate attributeName="r" values="${R + 12};${R + 20};${R + 12}" dur="3.2s" repeatCount="indefinite"/>
      <animate attributeName="opacity" values="${s.on ? 0.10 : 0.03};${s.on ? 0.16 : 0.05};${s.on ? 0.10 : 0.03}" dur="3.2s" repeatCount="indefinite"/>
    </circle>`;
    // Node disc
    svg += `<circle r="${R}" fill="rgba(10,12,16,0.92)" stroke="${s.color}" stroke-width="1.6"/>`;
    // Expanding pulse ring
    svg += `<circle r="${R}" fill="none" stroke="${s.color}" stroke-width="1" opacity="0.5">
      <animate attributeName="r" values="${R};${R + 18}" dur="2.6s" repeatCount="indefinite" begin="${i * 0.4}s"/>
      <animate attributeName="opacity" values="0.5;0" dur="2.6s" repeatCount="indefinite" begin="${i * 0.4}s"/>
    </circle>`;
    // Icon (center)
    svg += `<g transform="translate(0,-10)">${flowIcon(s.label, s.color)}</g>`;
    // Labels below node
    svg += `<text y="${R + 16}" text-anchor="middle" fill="#9aa0a6" font-size="10" letter-spacing="2" font-weight="600">${s.label}</text>`;
    svg += `<text y="${R + 32}" text-anchor="middle" fill="${s.on ? s.color : '#5f6368'}" font-size="13" font-weight="700"
      font-family="JetBrains Mono, monospace" filter="url(#flowGlow)">${s.value}</text>`;
    svg += `<text y="${R + 46}" text-anchor="middle" fill="#5f6368" font-size="9" font-family="JetBrains Mono, monospace">${s.sub}</text>`;
    svg += `</g></g>`;
  });

  svg += '</svg>';
  els.flowSvg.innerHTML = svg;
}

// --- Decision Tree ---
function renderDecisionTree(ctx, features, prediction, trades) {
  const nodes = [
    { type: 'input', label: 'WSSS Temp', value: features?.wsss_current_temp?.toFixed(1) + '°C' || '—' },
    { type: 'input', label: 'Dewpoint', value: features?.wsss_dewp?.toFixed(1) + '°C' || '—' },
    { type: 'input', label: 'Storm', value: features?.changi_forecast_storm ? 'YES' : 'No' },
    { type: 'process', label: 'Prediction', value: prediction ? `${prediction.mean_c}±${prediction.std_c}°C` : '—' },
    { type: 'process', label: 'Storm Score', value: features?.rain_dist_to_changi_km != null ? `~${features.rain_dist_to_changi_km.toFixed(1)}km` : '—' },
    { type: 'output', label: 'Edge', value: trades?.length ? `${(trades[0].edge * 100 || 0).toFixed(1)}%` : '—' },
  ];

  const actionNodes = [];
  if (trades?.length) {
    trades.slice(0, 4).forEach(t => {
      if (t.action?.startsWith('BUY_') || t.action?.startsWith('ENTER_')) {
        actionNodes.push({ type: 'action', label: t.bracket, value: t.action });
      }
    });
  }

  let html = nodes.map(n => `
    <div class="decision__node" data-type="${n.type}" data-active="${n.value !== '—'}">
      <span class="decision__node-label">${n.label}</span>
      <span class="decision__node-value">${n.value}</span>
    </div>
  `).join('');

  if (actionNodes.length) {
    html += actionNodes.map(n => `
      <div class="decision__node" data-type="${n.type}" data-active="true">
        <span class="decision__node-label">${n.label}</span>
        <span class="decision__node-value">${renderAction({ action: n.value })}</span>
      </div>
    `).join('');
  }

  els.decisionFlow.innerHTML = html || '<div class="emptystate">Waiting for data...</div>';
}

// --- WSSS Live ---
function renderWsss(wsss) {
  if (!wsss?.latest) return;

  const l = wsss.latest;
  els.wsssTime.textContent = l.obs_time_sgt || '—';
  els.wsssTemp.textContent = (l.temp != null ? l.temp.toFixed(1) + '°C' : '—');
  els.wsssFlight.textContent = l.flight_category || '—';
  els.wsssFlight.dataset.cat = l.flight_category || '';
  els.wsssWx.textContent = l.wxString || '—';
  els.wsssDewp.textContent = (l.dewp != null ? l.dewp.toFixed(1) + '°C' : '—');
  els.wsssRh.textContent = (l.rh != null ? l.rh.toFixed(0) + '%' : '—');
  els.wsssWind.textContent = (l.wspd != null ? l.wspd.toFixed(0) + ' kt' : '—');
  els.wsssGust.textContent = (l.gust != null ? l.gust.toFixed(0) + ' kt' : '—');
  els.wsssPres.textContent = (l.altim != null ? l.altim.toFixed(0) + ' hPa' : '—');
  els.wsssTrend.textContent = (l.press_trend_3h != null ? (l.press_trend_3h > 0 ? '+' : '') + l.press_trend_3h.toFixed(1) + ' hPa' : '—');
  els.wsssVis.textContent = (l.visib != null ? l.visib.toFixed(1) + ' km' : '—');
  els.wsssCloud.textContent = (l.cloud_oktas != null ? l.cloud_oktas.toFixed(0) + '/8' : '—');
  els.wsssCeil.textContent = (l.low_cloud_ft > 0 ? l.low_cloud_ft.toLocaleString() + ' ft' : '—');

  // Sparkline
  if (wsss.history?.length > 1) {
    const W = 280, H = 40;
    const temps = wsss.history.map(h => h.temp);
    const minT = Math.min(...temps) - 1;
    const maxT = Math.max(...temps) + 1;
    const xs = (i) => (i / (temps.length - 1)) * W;
    const ys = (t) => H - ((t - minT) / (maxT - minT)) * H;

    let path = '';
    temps.forEach((t, i) => {
      path += (i ? 'L' : 'M') + xs(i).toFixed(1) + ',' + ys(t).toFixed(1);
    });

    els.wsssSparkline.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
        <defs>
          <linearGradient id="sparkGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.3"/>
            <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
          </linearGradient>
        </defs>
        <path d="${path} L${W},${H} L0,${H} Z" fill="url(#sparkGrad)"/>
        <path d="${path}" fill="none" stroke="var(--accent)" stroke-width="1.5" stroke-linecap="round"/>
      </svg>
    `;
  }
}

// --- Map ---
function initMap() {
  if (_map) return;
  _map = L.map('map', {
    center: [1.352, 103.82],
    zoom: 11,
    zoomControl: true,
    attributionControl: false
  });
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19
  }).addTo(_map);
}

function renderMap(spatial) {
  if (!spatial?.layers) return;
  const layers = spatial.layers;

  // Remove existing layer
  if (_mapLayer) {
    _map.removeLayer(_mapLayer);
    _mapLayer = null;
  }

  const metric = _currentMetric;
  const data = layers[metric];

  if (!data) return;

  _mapLayer = L.layerGroup();

  // Color scale for temperature/rainfall
  const colorScale = (v, min, max) => {
    const t = (v - min) / (max - min);
    if (t < 0.33) return '#4285f4';
    if (t < 0.66) return '#fbbc04';
    return '#ea4335';
  };

  if (metric === 'air_temperature' && data.points) {
    const temps = data.points.map(p => p.value);
    const minT = Math.min(...temps), maxT = Math.max(...temps);
    data.points.forEach(p => {
      const color = colorScale(p.value, minT, maxT);
      L.circleMarker([p.lat, p.lon], {
        radius: 8,
        fillColor: color,
        fillOpacity: 0.8,
        color: '#fff',
        weight: 1
      }).bindPopup(`<b>${p.name}</b><br/>${p.value.toFixed(1)}°C`).addTo(_mapLayer);
    });
  } else if (metric === 'rainfall' && data.points) {
    const rainVals = data.points.map(p => p.value);
    const maxR = Math.max(...rainVals, 1);
    data.points.forEach(p => {
      if (p.value > 0) {
        L.circleMarker([p.lat, p.lon], {
          radius: 6 + p.value * 4,
          fillColor: '#4285f4',
          fillOpacity: 0.6 + p.value / maxR * 0.4,
          color: '#fff',
          weight: 1
        }).bindPopup(`<b>${p.name}</b><br/>${p.value.toFixed(1)} mm`).addTo(_mapLayer);
      }
    });
  } else if (metric === 'wind' && data.points) {
    data.points.forEach(p => {
      if (p.speed != null) {
        const arrow = L.divIcon({
          className: 'wind-arrow',
          html: `<div style="transform: rotate(${p.dir}deg); width: 20px; height: 20px; display: flex; align-items: center; justify-content: center;">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#8ab4f8" stroke-width="2">
              <path d="M12 2L8 10h3v12l4-8h-3V2z"/>
            </svg>
          </div>`,
          iconSize: [20, 20]
        });
        L.marker([p.lat, p.lon], { icon: arrow }).bindPopup(`<b>${p.name}</b><br/>${p.speed.toFixed(0)} kt @ ${p.dir.toFixed(0)}°`).addTo(_mapLayer);
      }
    });
  } else if (metric === 'lightning' && data.count > 0) {
    data.points.forEach(p => {
      L.circleMarker([p.lat, p.lon], {
        radius: 4,
        fillColor: '#fbbc04',
        fillOpacity: 0.9,
        color: '#fff',
        weight: 1
      }).bindPopup('Lightning strike').addTo(_mapLayer);
    });
  } else if (metric === 'uv') {
    L.marker([1.352, 103.82], {
      icon: L.divIcon({
        className: 'uv-chip',
        html: `<div style="background: var(--bg-card); padding: 6px 12px; border-radius: 4px; border: 1px solid var(--border); font-family: var(--font-mono); font-size: 12px;">
          UV: <b style="color: ${data.value >= 8 ? 'var(--danger)' : data.value >= 5 ? 'var(--warning)' : 'var(--success)'}">${data.value}</b>
        </div>`,
        iconSize: [80, 30]
      })
    }).addTo(_mapLayer);
  } else if (metric === 'wbgt') {
    L.marker([1.352, 103.82], {
      icon: L.divIcon({
        className: 'wbgt-chip',
        html: `<div style="background: var(--bg-card); padding: 6px 12px; border-radius: 4px; border: 1px solid var(--border); font-family: var(--font-mono); font-size: 12px;">
          WBGT: <b style="color: ${data.value >= 32 ? 'var(--danger)' : data.value >= 29 ? 'var(--warning)' : 'var(--success)'}">${data.value.toFixed(0)}</b>
        </div>`,
        iconSize: [80, 30]
      })
    }).addTo(_mapLayer);
  }

  _mapLayer.addTo(_map);
  els.mapTimestamp.textContent = spatial.generated_at_sgt || '—';

  // Fix any zero-size init (map rendered while container was hidden)
  if (_map && _map.invalidateSize) _map.invalidateSize();

  // Update legend
  renderLegend(metric, layers);
}

function renderLegend(metric, layers) {
  const legendItems = {
    air_temperature: { label: '°C', colors: ['#4285f4', '#fbbc04', '#ea4335'], range: 'cool→hot' },
    rainfall: { label: 'mm', colors: ['#4285f4'], range: 'light→heavy' },
    wind: { label: 'kt', colors: ['#8ab4f8'], range: 'direction + speed' },
    lightning: { label: 'strikes', colors: ['#fbbc04'], range: 'live' },
    uv: { label: 'index', colors: ['#34a853', '#fbbc04', '#ea4335'], range: 'low→high' },
    wbgt: { label: '°C', colors: ['#34a853', '#fbbc04', '#ea4335'], range: 'safe→danger' }
  };
  const l = legendItems[metric];
  if (!l) {
    els.mapLegend.innerHTML = '';
    return;
  }
  els.mapLegend.innerHTML = l.colors.map(c => `<span style="background:${c}"></span>`).join('') + ` ${l.range}`;
}

function setupMetricToggle(layers) {
  const metrics = [
    { id: 'air_temperature', label: 'Temp' },
    { id: 'rainfall', label: 'Rain' },
    { id: 'wind', label: 'Wind' },
    { id: 'lightning', label: 'Lightning' },
    { id: 'uv', label: 'UV' },
    { id: 'wbgt', label: 'WBGT' }
  ];

  els.metricToggle.innerHTML = metrics.map(m => `
    <button class="metric-toggle__btn" data-metric="${m.id}" data-active="${m.id === _currentMetric}">${m.label}</button>
  `).join('');

  els.metricToggle.querySelectorAll('button').forEach(btn => {
    btn.addEventListener('click', () => {
      _currentMetric = btn.dataset.metric;
      els.metricToggle.querySelectorAll('button').forEach(b => b.dataset.active = 'false');
      btn.dataset.active = 'true';
      // Re-fetch spatial data for new metric
      loadSpatial();
    });
  });
}

// --- Tables ---
function renderPositions(positions) {
  if (!positions?.length) {
    els.positionsBody.innerHTML = '<tr><td colspan="6" class="emptystate">No open positions</td></tr>';
    return;
  }
  els.positionsBody.innerHTML = positions.map(p => {
    const entry = p.entry_price != null ? '¢' + (p.entry_price * 100).toFixed(1) : '—';
    const now = p.exit_price != null ? '¢' + (p.exit_price * 100).toFixed(1) : '—';
    const pnl = p.pnl_pct != null ? (p.pnl_pct * 100).toFixed(1) + '%' : '—';
    const pnlCls = !p.pnl_pct ? '' : p.pnl_pct >= 0 ? 'green' : 'red';
    return `<tr>
      <td>${escapeHtml(p.bracket || '')}</td>
      <td>${renderAction({ action: p.side })}</td>
      <td class="num">${entry}</td>
      <td class="num">${now}</td>
      <td class="num ${pnlCls}">${pnl}</td>
      <td>${renderAction(p)}</td>
    </tr>`;
  }).join('');
}

function renderBrackets(event) {
  if (!event || event.error) {
    els.bracketsBody.innerHTML = `<tr><td colspan="7" class="emptystate">${escapeHtml((event && event.error) || 'No event data.')}</td></tr>`;
    els.bracketsFoot.textContent = '';
    return;
  }
  const trades = event.trades || [];
  els.eventDate.textContent = event.date_str || '—';
  els.bracketsFoot.textContent = `${trades.length} bracket(s)`;

  if (!trades.length) {
    els.bracketsBody.innerHTML = '<tr><td colspan="7" class="emptystate">No brackets</td></tr>';
    _lastModelProbs = [];
    return;
  }

  _lastModelProbs = trades.map(t => t.prob != null ? t.prob : null);
  _prevYes = trades.map(t => t.yes_price != null ? t.yes_price * 100 : null);
  _prevNo = trades.map(t => t.no_price != null ? t.no_price * 100 : null);

  els.bracketsBody.innerHTML = trades.map(t => {
    const stake = t.stake_usd != null ? '$' + Number(t.stake_usd).toFixed(2) : '—';
    const prob = t.prob != null ? (t.prob * 100).toFixed(1) + '%' : '—';
    return `<tr>
      <td>${escapeHtml(t.bracket || '')}</td>
      <td class="num">${prob}</td>
      <td class="num yes">${t.yes_price != null ? '¢' + (t.yes_price * 100).toFixed(1) : '—'}</td>
      <td class="num no">${t.no_price != null ? '¢' + (t.no_price * 100).toFixed(1) : '—'}</td>
      <td>${renderAction(t)}</td>
      <td class="num">${t.edge != null ? (t.edge * 100).toFixed(1) + '%' : '—'}</td>
      <td class="num">${stake}</td>
    </tr>`;
  }).join('');
}

function renderAction(trade) {
  const cls = (trade.action || '').toUpperCase();
  return `<span class="tag tag--${cls}">${escapeHtml(cls)}</span>`;
}

// --- Prediction Chart ---
function renderPredictionChart(prediction) {
  if (!prediction) return;
  const mu = prediction.mean_c, sigma = Math.max(0.15, prediction.std_c);
  const W = 260, H = 70, padL = 30, padR = 10, padT = 10, padB = 20;
  const lo = mu - 3.4 * sigma, hi = mu + 3.4 * sigma;
  const xs = (t) => padL + ((t - lo) / (hi - lo)) * (W - padL - padR);
  const ymax = 1 / (sigma * Math.sqrt(2 * Math.PI));
  const ys = (y) => H - padB - (y / ymax) * (H - padT - padB);
  const pdf = (x) => Math.exp(-0.5 * ((x - mu) / sigma) ** 2) / (sigma * Math.sqrt(2 * Math.PI));

  const n = 60;
  let line = '';
  for (let i = 0; i <= n; i++) {
    const x = lo + (i / n) * (hi - lo);
    line += (i ? ' L' : 'M') + xs(x).toFixed(1) + ' ' + ys(pdf(x)).toFixed(1);
  }
  const area = line + ` L${xs(hi).toFixed(1)} ${ys(0).toFixed(1)} L${xs(lo).toFixed(1)} ${ys(0).toFixed(1)} Z`;

  els.predChart.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
      <path d="${area}" fill="var(--accent)" opacity="0.15"/>
      <path d="${line}" fill="none" stroke="var(--accent)" stroke-width="1.5"/>
      <line x1="${xs(mu).toFixed(1)}" y1="${padT}" x2="${xs(mu).toFixed(1)}" y2="${H - padB}" stroke="var(--accent)" stroke-width="1" stroke-dasharray="2 2"/>
      <text x="${xs(mu).toFixed(1)}" y="${padT - 2}" fill="var(--text)" font-size="9" text-anchor="middle" font-family="var(--font-mono)">${mu.toFixed(1)}°C</text>
    </svg>
  `;
}

// --- Price Updates ---
function patchPrices(brackets) {
  const rows = els.bracketsBody.querySelectorAll('tr');
  brackets.forEach((b, i) => {
    if (rows[i]) {
      const cells = rows[i].querySelectorAll('td');
      const yes = b.yes != null && b.yes > 0 && b.yes <= 1 ? b.yes : null;
      const no = b.no != null && b.no > 0 && b.no <= 1 ? b.no : null;
      const yesC = yes != null ? yes * 100 : null;
      const noC = no != null ? no * 100 : null;
      if (cells[2]) cells[2].textContent = yesC != null ? '¢' + yesC.toFixed(1) : '—';
      if (cells[3]) cells[3].textContent = noC != null ? '¢' + noC.toFixed(1) : '—';
    }
  });
  _priceTicks++;
  els.tickCount.textContent = _priceTicks + ' ticks';
}

// --- API Calls ---
async function loadDashboard() {
  try {
    const res = await fetch('/api/dashboard?fresh=true', { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();

    // Update system status
    setSystemStatus('ok', 'LIVE');

    // Prediction
    if (data.prediction) {
      els.predValue.textContent = data.prediction.mean_c?.toFixed(1) + '°C' || '—';
      els.predMeta.textContent = `±${data.prediction.std_c?.toFixed(2)}°C · hour ${data.prediction.hour_of_day ?? '—'} SGT`;
      renderPredictionChart(data.prediction);
    }

    // Context
    if (data.context?.length) {
      els.contextList.innerHTML = data.context.map(c => `<span>${escapeHtml(c)}</span>`).join('');
    } else {
      els.contextList.innerHTML = '<span>No signals</span>';
    }

    // Data flow
    renderDataFlow(data);

    // Decision tree
    renderDecisionTree(data.context, data.features, data.prediction, data.event?.trades);

    // Tables
    renderPositions(data.positions);
    renderBrackets(data.event);

  } catch (err) {
    setSystemStatus('error', 'ERROR');
    console.error('Dashboard error:', err);
  }
}

let _spatialSeq = 0;
async function loadSpatial() {
  const seq = ++_spatialSeq;
  try {
    const res = await fetch('/api/spatial', { cache: 'no-store' });
    if (!res.ok || seq < _spatialSeq) return;
    const data = await res.json();

    // Initialize map on first load
    if (!_map) initMap();

    // Setup toggle if not done
    if (!els.metricToggle.querySelector('[data-metric]')) {
      setupMetricToggle(data.layers);
    }

    renderMap(data);
  } catch (err) {
    console.error('Spatial error:', err);
  }
}

async function loadWsss() {
  try {
    const res = await fetch('/api/wsss', { cache: 'no-store' });
    if (!res.ok) return;
    const data = await res.json();
    renderWsss(data);
  } catch (err) {
    console.error('WSSS error:', err);
  }
}

async function loadPrices() {
  const seq = ++_priceSeq;
  try {
    const res = await fetch('/api/prices', { cache: 'no-store' });
    if (!res.ok || seq < _priceSeq) return;
    const data = await res.json();

    // Feed status
    if (data.feed?.connected) {
      els.feedStatus.dataset.state = 'ok';
      els.feedStatus.querySelector('span:last-child').textContent = 'Connected';
    }

    if (data.positions) renderPositions(data.positions);
    if (data.brackets) patchPrices(data.brackets);

  } catch (err) {
    // Silent
  }
}

// --- Utils ---
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

// --- Boot ---
let _fullCountdown = POLL_FULL;
function tickCountdown() {
  _fullCountdown--;
  if (_fullCountdown <= 0) {
    _fullCountdown = POLL_FULL;
    loadDashboard();
  }
}

// Initialize
setInterval(tickClock, 1000);
setInterval(tickCountdown, 1000);
els.refresh.addEventListener('click', () => { _fullCountdown = POLL_FULL; loadDashboard(); });

tickClock();
loadDashboard();
loadSpatial();
loadWsss();

setInterval(loadPrices, POLL_PRICES * 1000);
setInterval(loadSpatial, POLL_SPATIAL * 1000);
setInterval(loadWsss, POLL_WSSS * 1000);