/**
 * SRE Copilot Dashboard — app.js
 *
 * Responsibilities:
 *  - Sidebar navigation + view switching
 *  - Poll /api/v1/dashboard/summary every 15s
 *  - Load /api/v1/incidents, /api/v1/alerts
 *  - Open WS ws://host/ws, handle all server→client events
 *  - Incident detail page: report, timeline, suggested fixes, live findings
 *  - Human-in-the-loop action card: show on analysis.awaiting_human,
 *    send human_input via WS on submit
 */

// ── Config ──────────────────────────────────────────────────────────────────

const API   = '/api/v1';
let TOKEN = localStorage.getItem('sre_token') || '';

if (!TOKEN) {
  const inputToken = prompt('Please enter your SRE_INTERNAL_TOKEN:');
  if (inputToken) {
    TOKEN = inputToken;
    localStorage.setItem('sre_token', TOKEN);
  }
}
const WS_RECONNECT_DELAY_MS = 3000;

// ── State ────────────────────────────────────────────────────────────────────

let ws            = null;
let wsReconnectTimer = null;
let currentView   = 'overview';
let currentIncident = null;   // incident detail currently shown
let pendingHITL   = null;      // { analysis_id, interrupt_type, question, context, timeout_at }

// ── Helpers ──────────────────────────────────────────────────────────────────

function headers() {
  const h = { 'Content-Type': 'application/json' };
  if (TOKEN) h['Authorization'] = `Bearer ${TOKEN}`;
  return h;
}

async function apiFetch(path, opts = {}) {
  const resp = await fetch(API + path, { headers: headers(), ...opts });
  if (resp.status === 401) {
    localStorage.removeItem('sre_token');
    alert('Unauthorized: Invalid or missing token. Token cleared. Please refresh the page to try again.');
    throw new Error('401 Unauthorized: Invalid token');
  }
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${resp.status} ${path}: ${text.slice(0, 120)}`);
  }
  return resp.json();
}

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function fmtDateTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function shortID(id) {
  return id ? id.slice(0, 8) : '—';
}

function statusBadge(status) {
  const cls = {
    firing: 'badge-firing', resolved: 'badge-resolved',
    open: 'badge-open', pending: 'badge-pending', running: 'badge-running',
    completed: 'badge-completed', failed: 'badge-failed',
    awaiting_human: 'badge-awaiting', cancelled: 'badge-cancelled',
    suppressed: 'badge-suppressed',
  };
  return `<span class="badge ${cls[status] || ''}">${status || '—'}</span>`;
}

function sevBadge(sev) {
  return `<span class="sev-badge sev-${sev || 'info'}">${sev || 'info'}</span>`;
}

function toast(msg, type = 'info') {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

// ── Navigation ────────────────────────────────────────────────────────────────

const VIEWS = ['overview', 'incidents', 'alerts', 'analyses', 'incident-detail'];
const VIEW_TITLES = {
  overview: ['Overview', 'Live system intelligence'],
  incidents: ['Incidents', 'Active + historical incidents'],
  alerts:    ['Alerts', 'Firing alerts from all sources'],
  analyses:  ['Analyses', 'LangGraph investigation runs'],
  'incident-detail': ['Incident Detail', 'Full report & timeline'],
};

function showView(name) {
  currentView = name;
  VIEWS.forEach(v => {
    document.getElementById(`view-${v}`).style.display = v === name ? '' : 'none';
  });
  document.querySelectorAll('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.view === name);
  });
  const [title, sub] = VIEW_TITLES[name] || ['', ''];
  document.getElementById('page-title').textContent = title;
  document.getElementById('page-subtitle').textContent = sub;

  // Load data for view
  if (name === 'incidents') loadIncidents();
  if (name === 'alerts')    loadAlerts();
  if (name === 'analyses')  loadAllAnalyses();
}

document.querySelectorAll('.nav-item[data-view]').forEach(el => {
  el.addEventListener('click', () => showView(el.dataset.view));
});
document.getElementById('btn-back').addEventListener('click', () => showView('incidents'));
document.getElementById('btn-refresh').addEventListener('click', () => {
  loadSummary();
  if (currentView !== 'overview' && currentView !== 'incident-detail') {
    showView(currentView);
  }
});

// Stat card clicks
document.getElementById('card-open-incidents').addEventListener('click', () => showView('incidents'));
document.getElementById('card-firing-alerts').addEventListener('click', () => showView('alerts'));
document.getElementById('card-analyses').addEventListener('click', () => showView('analyses'));

// ── Summary ──────────────────────────────────────────────────────────────────

async function loadSummary() {
  try {
    const data = await apiFetch('/dashboard/summary');

    document.getElementById('stat-open-incidents').textContent = data.open_incidents ?? '—';
    document.getElementById('stat-firing-alerts').textContent  = data.firing_alerts  ?? '—';
    document.getElementById('stat-analyses').textContent       = (data.recent_analyses || []).length;
    document.getElementById('badge-incidents').textContent     = data.open_incidents ?? 0;
    document.getElementById('badge-alerts').textContent        = data.firing_alerts  ?? 0;
    document.getElementById('badge-analyses-count').textContent = (data.recent_analyses || []).length;
    document.getElementById('last-updated').textContent        = 'Updated ' + fmtTime(data.generated_at);

    renderAnalysesTable(data.recent_analyses || [], 'analyses-tbody');
  } catch (e) {
    console.error('summary load failed:', e);
  }
}

// ── System health ─────────────────────────────────────────────────────────────

async function loadSystemHealth() {
  try {
    const data = await apiFetch('/health');
    const el   = document.getElementById('stat-system-status');
    const hint = document.getElementById('stat-system-hint');
    el.textContent   = data.status === 'ok' ? '✓ Healthy' : '⚠ Degraded';
    hint.textContent = `Up ${Math.floor((data.uptime_seconds || 0) / 60)}m`;
    el.style.color = data.status === 'ok' ? 'var(--success)' : 'var(--warning)';
  } catch (e) {
    document.getElementById('stat-system-status').textContent = 'Unavailable';
  }
}

// ── Analyses table renderer ───────────────────────────────────────────────────

function renderAnalysesTable(analyses, tbodyId) {
  const tbody = document.getElementById(tbodyId);
  if (!analyses.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-row">No analyses yet</td></tr>';
    return;
  }
  tbody.innerHTML = analyses.map(a => `
    <tr>
      <td class="mono">${shortID(a.analysis_id)}</td>
      <td class="mono">${shortID(a.alert_id)}</td>
      <td>${statusBadge(a.status)}</td>
      <td>${fmtDateTime(a.started_at)}</td>
      <td>${a.completed_at ? fmtDateTime(a.completed_at) : '—'}</td>
    </tr>
  `).join('');
}

// ── Incidents ────────────────────────────────────────────────────────────────

async function loadIncidents() {
  try {
    const data = await apiFetch('/incidents?status=open&page_size=50');
    const tbody = document.getElementById('incidents-tbody');
    const list = data.incidents || [];
    if (!list.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty-row">No open incidents</td></tr>';
      return;
    }
    tbody.innerHTML = list.map(i => `
      <tr>
        <td class="mono">${shortID(i.incident_id)}</td>
        <td>${i.title || '—'}</td>
        <td>${sevBadge(i.severity)}</td>
        <td>${statusBadge(i.status)}</td>
        <td>${(i.affected_services || []).join(', ') || '—'}</td>
        <td>${fmtDateTime(i.opened_at)}</td>
        <td><button class="btn btn-ghost" onclick="openIncident('${i.incident_id}')">View →</button></td>
      </tr>
    `).join('');
  } catch (e) {
    document.getElementById('incidents-tbody').innerHTML =
      `<tr><td colspan="7" class="empty-row" style="color:var(--danger)">Error: ${e.message}</td></tr>`;
  }
}

// ── Alerts ────────────────────────────────────────────────────────────────────

async function loadAlerts() {
  try {
    const data = await apiFetch('/alerts?status=firing&page_size=100');
    const tbody = document.getElementById('alerts-tbody');
    const list = data.alerts || [];
    if (!list.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty-row">No firing alerts</td></tr>';
      return;
    }
    tbody.innerHTML = list.map(a => `
      <tr>
        <td class="mono">${shortID(a.alert_id)}</td>
        <td>${a.name || '—'}</td>
        <td><span class="badge">${a.source || '—'}</span></td>
        <td>${sevBadge(a.severity)}</td>
        <td>${fmtDateTime(a.fired_at)}</td>
        <td>${(a.affected_services || []).join(', ') || '—'}</td>
      </tr>
    `).join('');
  } catch (e) {
    document.getElementById('alerts-tbody').innerHTML =
      `<tr><td colspan="6" class="empty-row" style="color:var(--danger)">Error: ${e.message}</td></tr>`;
  }
}

// ── All Analyses ──────────────────────────────────────────────────────────────

async function loadAllAnalyses() {
  try {
    const data = await apiFetch('/incidents?page_size=100');   // fallback via incidents
    // Actually load from summary analyses
    const sum = await apiFetch('/dashboard/summary');
    renderAnalysesTable(sum.recent_analyses || [], 'all-analyses-tbody');
  } catch (e) {
    document.getElementById('all-analyses-tbody').innerHTML =
      `<tr><td colspan="5" class="empty-row" style="color:var(--danger)">Error: ${e.message}</td></tr>`;
  }
}

// ── Incident detail ───────────────────────────────────────────────────────────

async function openIncident(incidentId) {
  currentIncident = incidentId;
  showView('incident-detail');

  const container = document.getElementById('incident-detail-content');
  container.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-muted)">Loading…</div>';

  // Subscribe via WS for live updates
  wsSend({ event: 'subscribe.incident', payload: { incident_id: incidentId } });

  try {
    const inc = await apiFetch(`/incidents/${incidentId}`);
    renderIncidentDetail(inc);
  } catch (e) {
    container.innerHTML = `<div class="card" style="color:var(--danger)">Failed to load: ${e.message}</div>`;
  }
}

function renderIncidentDetail(inc) {
  const container = document.getElementById('incident-detail-content');
  const report = inc.report;
  const events = inc.events || [];

  container.innerHTML = `
    <!-- Header -->
    <div class="card">
      <div class="incident-header">
        <div>
          <div class="incident-title">${inc.title || 'Untitled Incident'}</div>
          <div class="incident-meta">
            ${sevBadge(inc.severity)}
            ${statusBadge(inc.status)}
            <span class="mono" style="font-size:12px">ID: ${inc.incident_id}</span>
            <span style="font-size:12px;color:var(--text-muted)">Opened: ${fmtDateTime(inc.opened_at)}</span>
            ${inc.resolved_at ? `<span style="font-size:12px;color:var(--success)">Resolved: ${fmtDateTime(inc.resolved_at)}</span>` : ''}
          </div>
          ${(inc.affected_services || []).length ? `
            <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
              ${inc.affected_services.map(s => `<span class="badge">${s}</span>`).join('')}
            </div>` : ''}
        </div>
      </div>
      ${report ? `<div style="margin-top:16px;padding:16px;background:var(--bg-elevated);border-radius:var(--radius-sm)">
        <div style="font-size:12px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">Executive Summary</div>
        <div style="font-size:14px;line-height:1.6;color:var(--text-primary)">${report.executive_summary || '—'}</div>
      </div>` : ''}
    </div>

    ${report && report.root_cause ? `
    <div class="card">
      <div class="card-header"><h2>Root Cause</h2>
        <span class="sev-badge sev-${report.root_cause.confidence > 0.7 ? 'medium' : 'low'}">
          confidence ${Math.round((report.root_cause.confidence || 0) * 100)}%
        </span>
      </div>
      <div style="font-size:14px;line-height:1.6">${report.root_cause.description || '—'}</div>
      ${(report.root_cause.affected_services || []).length ? `
        <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
          ${report.root_cause.affected_services.map(s => `<span class="badge">${s}</span>`).join('')}
        </div>` : ''}
    </div>` : ''}

    ${report && (report.suggested_fixes || []).length ? `
    <div class="card">
      <div class="card-header"><h2>Suggested Fixes</h2></div>
      ${report.suggested_fixes.sort((a,b) => (a.priority||99) - (b.priority||99)).map(fix => `
        <div class="fix-card">
          <div class="fix-priority">${fix.priority || '?'}</div>
          <div style="flex:1">
            <div class="fix-action">${fix.action || '—'}</div>
            <div class="fix-rationale">${fix.rationale || ''}</div>
            ${fix.runbook_reference ? `<div style="font-size:11px;margin-top:4px;color:var(--text-code)">📖 ${fix.runbook_reference}</div>` : ''}
            <span class="fix-risk risk-${fix.risk_level || 'low'}">${fix.risk_level || 'low'} risk</span>
          </div>
        </div>
      `).join('')}
    </div>` : ''}

    ${events.length ? `
    <div class="card">
      <div class="card-header"><h2>Findings Timeline</h2>
        <span class="card-badge">${events.length}</span>
      </div>
      <div id="incident-findings">
        ${events.map(f => renderFinding(f)).join('')}
      </div>
    </div>` : ''}

    ${report && (report.timeline || []).length ? `
    <div class="card">
      <div class="card-header"><h2>Incident Timeline</h2></div>
      <ul class="timeline">
        ${report.timeline.map(t => `
          <li class="timeline-item">
            <div class="timeline-ts">${fmtDateTime(t.timestamp)}</div>
            <div class="timeline-event">${t.event || '—'}</div>
            <div class="timeline-source">${t.source || ''}</div>
          </li>
        `).join('')}
      </ul>
    </div>` : ''}
  `;
}

function renderFinding(f) {
  return `
    <div class="finding-card" id="finding-${f.finding_id}">
      <div class="finding-header">
        <span class="badge" style="font-size:11px">${f.type || '—'}</span>
        ${sevBadge(f.severity)}
        <span style="font-size:11px;color:var(--text-muted)">${f.agent || ''}</span>
        <span style="margin-left:auto;font-size:11px;color:var(--text-muted)">${fmtDateTime(f.created_at)}</span>
      </div>
      <div class="finding-title">${f.title || '—'}</div>
      <div class="finding-summary">${f.summary || ''}</div>
      <div class="finding-confidence">Confidence: ${Math.round((f.confidence || 0) * 100)}%</div>
    </div>
  `;
}

// ── Human-in-the-loop banner ──────────────────────────────────────────────────

function showHITLBanner(payload) {
  pendingHITL = payload;
  const banner  = document.getElementById('hitl-banner');
  document.getElementById('hitl-question').textContent  = payload.question || 'The AI needs your guidance.';
  document.getElementById('hitl-type').textContent       = payload.interrupt_type || '';
  document.getElementById('hitl-timeout').textContent    = payload.timeout_at
    ? `Timeout: ${fmtDateTime(payload.timeout_at)}` : '';

  // Build action UI depending on interrupt_type
  const actionsEl = document.getElementById('hitl-actions');
  actionsEl.innerHTML = '';

  const makeBtn = (label, cls, onclick) => {
    const b = document.createElement('button');
    b.className = `btn ${cls}`;
    b.textContent = label;
    b.onclick = onclick;
    return b;
  };

  const type = payload.interrupt_type;

  if (type === 'approve_fix' || type === 'reject_fix') {
    // Show fix index 0 approve/reject
    actionsEl.appendChild(makeBtn('✓ Approve', 'btn-primary', () => submitHITL({ fix_index: 0 })));
    actionsEl.appendChild(makeBtn('✗ Reject', 'btn-ghost', () => {
      const reason = prompt('Reason for rejection?', '');
      submitHITL({ fix_index: 0, reason: reason || '' });
    }));
  } else if (type === 'override_root_cause') {
    const input = document.createElement('textarea');
    input.placeholder = 'Describe the actual root cause…';
    input.style.cssText = 'width:100%;min-height:70px;background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--radius-sm);color:var(--text-primary);padding:8px;font-family:var(--font-sans);font-size:13px;resize:vertical;';
    actionsEl.appendChild(input);
    actionsEl.appendChild(makeBtn('Submit Override', 'btn-warning', () => submitHITL({ root_cause: input.value })));
  } else {
    // provide_context (default)
    const input = document.createElement('textarea');
    input.placeholder = 'Provide additional context for the AI…';
    input.style.cssText = 'width:100%;min-height:70px;background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--radius-sm);color:var(--text-primary);padding:8px;font-family:var(--font-sans);font-size:13px;resize:vertical;';
    actionsEl.appendChild(input);
    actionsEl.appendChild(makeBtn('Send Context', 'btn-primary', () => submitHITL({ message: input.value })));
  }

  actionsEl.appendChild(makeBtn('Dismiss', 'btn-ghost', hideHITLBanner));

  banner.style.display = 'flex';
  // Auto-navigate to relevant incident
  if (payload.incident_id) openIncident(payload.incident_id);
}

function hideHITLBanner() {
  document.getElementById('hitl-banner').style.display = 'none';
  pendingHITL = null;
}

function submitHITL(responsePayload) {
  if (!pendingHITL) return;
  wsSend({
    event: 'human_input',
    payload: {
      analysis_id:      pendingHITL.analysis_id,
      interrupt_type:   pendingHITL.interrupt_type,
      response_payload: responsePayload,
      provided_by:      'dashboard-user',
    },
  });
  toast('Human input sent to AI', 'success');
  hideHITLBanner();
}

// ── Event feed ────────────────────────────────────────────────────────────────

function pushEvent(type, text) {
  const feed = document.getElementById('event-feed');
  const li   = document.createElement('li');
  li.className = 'event-item';
  li.innerHTML = `
    <span class="event-time">${fmtTime(new Date().toISOString())}</span>
    <span class="event-type">${type}</span>
    <span class="event-text">${text}</span>
  `;
  feed.prepend(li);
  // Keep max 50 events
  while (feed.children.length > 50) feed.removeChild(feed.lastChild);
}

// ── WebSocket ─────────────────────────────────────────────────────────────────

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url   = `${proto}://${location.host}/ws${TOKEN ? `?token=${TOKEN}` : ''}`;

  setWSStatus('connecting');
  ws = new WebSocket(url);

  ws.onopen = () => {
    setWSStatus('connected');
    // Re-subscribe to current incident if viewing detail
    if (currentIncident) {
      wsSend({ event: 'subscribe.incident', payload: { incident_id: currentIncident } });
    }
  };

  ws.onmessage = (evt) => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch { return; }
    handleWSEvent(msg);
  };

  ws.onclose = () => {
    setWSStatus('disconnected');
    wsReconnectTimer = setTimeout(connectWS, WS_RECONNECT_DELAY_MS);
  };

  ws.onerror = () => {
    ws.close();
  };
}

function wsSend(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
  }
}

function setWSStatus(state) {
  const dot   = document.querySelector('.ws-dot');
  const label = document.getElementById('ws-label');
  dot.className = `ws-dot ${state}`;
  label.textContent = { connected: 'Live', disconnected: 'Disconnected', connecting: 'Connecting…' }[state] || state;
}

function handleWSEvent(msg) {
  const event   = msg.event;
  const payload = msg.payload || {};

  switch (event) {
    case 'ping':
      wsSend({ event: 'pong', payload: {} });
      break;

    case 'alert.fired':
      pushEvent('alert.fired', `${payload.name || '—'} [${payload.severity || ''}] on ${(payload.affected_services || []).join(', ') || '—'}`);
      toast(`🔔 Alert fired: ${payload.name || '—'}`, 'error');
      loadSummary();
      if (currentView === 'alerts') loadAlerts();
      break;

    case 'alert.resolved':
      pushEvent('alert.resolved', `Alert ${shortID(payload.alert_id)} resolved`);
      loadSummary();
      break;

    case 'analysis.started':
      pushEvent('analysis.started', `Analysis ${shortID(payload.analysis_id)} — incident ${shortID(payload.incident_id)}`);
      loadSummary();
      break;

    case 'analysis.agent_switched':
      pushEvent('analysis.agent_switched', `→ ${payload.to_agent || '?'} (reason: ${payload.reason || '—'})`);
      break;

    case 'analysis.finding':
      pushEvent('analysis.finding', `${(payload.finding || {}).type || '?'}: ${(payload.finding || {}).title || ''}`);
      // If viewing the incident that received this finding, inject it live
      if (currentIncident && payload.incident_id === currentIncident) {
        const container = document.getElementById('incident-findings');
        if (container) {
          const el = document.createElement('div');
          el.innerHTML = renderFinding(payload.finding || {});
          container.prepend(el.firstElementChild);
        }
      }
      break;

    case 'analysis.awaiting_human':
      pushEvent('analysis.awaiting_human', `⚠ AI paused — needs human input (${payload.interrupt_type})`);
      showHITLBanner(payload);
      toast('🧠 AI is waiting for your input', 'info');
      break;

    case 'analysis.completed':
      pushEvent('analysis.completed', `Analysis ${shortID(payload.analysis_id)} done — report ${shortID(payload.report_id)}`);
      toast('✓ Analysis complete — report ready', 'success');
      hideHITLBanner();
      loadSummary();
      // Reload incident detail if viewing it
      if (currentIncident && payload.incident_id === currentIncident) {
        openIncident(currentIncident);
      }
      break;

    case 'analysis.failed':
      pushEvent('analysis.failed', `Analysis ${shortID(payload.analysis_id)} failed: ${payload.error_message || '—'}`);
      toast(`✗ Analysis failed: ${payload.error_message || '—'}`, 'error');
      hideHITLBanner();
      break;

    case 'incident.updated':
      pushEvent('incident.updated', `Incident ${shortID(payload.incident_id)} updated`);
      if (currentView === 'incidents') loadIncidents();
      loadSummary();
      break;
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────

function init() {
  showView('overview');
  loadSummary();
  loadSystemHealth();
  connectWS();

  // Periodic refresh
  setInterval(loadSummary, 15_000);
  setInterval(loadSystemHealth, 30_000);
}

document.addEventListener('DOMContentLoaded', init);

// Expose openIncident globally (called from inline onclick in table)
window.openIncident = openIncident;
