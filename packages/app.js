// HerdR Dev Fabric control-plane web app (spec 0190 §5, §20-34, §42-47).
// Hash-routed SPA. The UI is a *client* of the structured /api/v1 JSON + SSE
// stream. Views render loading / empty / error / data states (Spec §91).
import { HerdrClient } from './herdr-web-client/client.js';
import { connectEvents } from './herdr-web-client/events.js';
import { formatBytes, formatDuration, formatTimestamp } from './herdr-web-components/format.js';
import { defineComponents } from './herdr-web-components/index.js';

defineComponents();

const client = new HerdrClient('');
const root = document.getElementById('view');

const NAV = [
  { path: '/overview', label: 'Overview' },
  { path: '/missions', label: 'Missions' },
  { path: '/herd', label: 'Sessions' },
  { path: '/services', label: 'Services' },
  { path: '/hosts', label: 'Hosts' },
  { path: '/activity', label: 'Activity' },
  { path: '/analytics', label: 'Analytics' },
];

const SHELL = document.getElementById('shell');
SHELL.setAttribute('routes', JSON.stringify(NAV));
SHELL.setAttribute('brand', 'HerdR Dev Fabric');

function route() {
  const hash = window.location.hash.replace(/^#/, '') || '/overview';
  const [path, query] = hash.split('?');
  const parts = path.split('/').filter(Boolean); // ['missions', id]
  return { path: path || '/overview', parts, query: new URLSearchParams(query) };
}

const ROUTES = {
  '/overview': viewOverview,
  '/missions': viewMissions,
  '/missions/:id': viewMission,
  '/herd': viewSessions,
  '/herd/session/:id': viewSession,
  '/services': viewServices,
  '/services/:id': viewService,
  '/hosts': viewHosts,
  '/hosts/:id': viewHost,
  '/activity': viewActivity,
  '/analytics': viewAnalytics,
  '/analytics/missions': viewAnalyticsMissions,
  '/analytics/models': viewAnalyticsModels,
  '/analytics/hosts': viewAnalyticsHosts,
};

async function render() {
  const { parts } = route();
  const seg = parts.length ? parts.join('/') : 'overview';
  const match = Object.keys(ROUTES).find((r) => {
    const rp = r.split('/').filter(Boolean);
    if (rp.length !== parts.length) return false;
    return rp.every((rpseg, i) => rpseg.startsWith(':') || rpseg === parts[i]);
  });
  root.innerHTML = '<herdr-spinner label="Loading"></herdr-spinner>';
  try {
    await ROUTES[match || '/overview'](parts);
  } catch (err) {
    root.innerHTML = `<herdr-error message="${esc(err.message || 'request failed')}"></herdr-error>`;
  }
}

// ------------------------------------------------------------- views
async function viewOverview() {
  const [health, missions, sessions, hosts] = await Promise.all([
    client.health(), client.listMissions(), client.listSessions(), client.listHosts(),
  ]);
  const active = sessions.sessions.filter((s) => s.status === 'active').length;
  root.innerHTML = `
    <h1>Overview</h1>
    <div class="hd-grid">
      <herdr-metric label="Missions" value="${missions.missions.length}"></herdr-metric>
      <herdr-metric label="Active sessions" value="${active}"></herdr-metric>
      <herdr-metric label="Hosts" value="${hosts.hosts.length}"></herdr-metric>
      <herdr-metric label="Events ingested" value="${health.events.ingested}"></herdr-metric>
    </div>
    <h3 style="margin-top:20px">Recent activity</h3>
    ${await activityList(10)}`;
}

async function viewMissions() {
  const { missions } = await client.listMissions();
  if (!missions.length) { root.innerHTML = '<herdr-empty message="No missions yet"></herdr-empty>'; return; }
  root.innerHTML = '<h1>Missions</h1><div class="hd-grid">' + missions.map((m) => `
    <div class="hd-card">
      <div class="hd-card__title"><a href="#/missions/${esc(m.mission_id)}">${esc(m.title)}</a></div>
      <div style="margin:6px 0"><herdr-status stage="${esc(m.status)}"></herdr-status></div>
      <div class="hd-card__sub">${m.sessions ? m.sessions.length : 0} session(s) · created ${formatTimestamp(m.created_at)}</div>
    </div>`).join('') + '</div>';
}

async function viewMission(parts) {
  const m = await client.getMission(parts[1]);
  if (!m) { root.innerHTML = '<herdr-error message="Mission not found"></herdr-error>'; return; }
  const sessions = m.sessions.length;
  root.innerHTML = `
    <h1>${esc(m.title)}</h1>
    <div style="margin:8px 0"><herdr-status stage="${esc(m.status)}"></herdr-status>
      <span class="hd-card__sub"> · <herdr-ident kind="mission" id="${esc(m.mission_id)}"></herdr-ident></span></div>
    <p>${esc(m.purpose || '—')}</p>
    <div class="hd-grid">
      <herdr-metric label="Sessions" value="${sessions}"></herdr-metric>
      <herdr-metric label="Stage" value="${esc(m.stage)}"></herdr-metric>
    </div>
    <h3 style="margin-top:20px">Event stream</h3>
    <div id="mission-stream">${await streamList('mission', m.mission_id)}</div>`;
}

async function viewSessions() {
  const { sessions } = await client.listSessions();
  if (!sessions.length) { root.innerHTML = '<herdr-empty message="No sessions yet"></herdr-empty>'; return; }
  root.innerHTML = '<h1>Sessions</h1><div class="hd-grid">' + sessions.map((s) => `
    <div class="hd-card">
      <div class="hd-card__title"><a href="#/herd/session/${esc(s.session_id)}">${esc(s.agent_role || 'session')}</a></div>
      <div style="margin:6px 0"><herdr-status stage="${esc(s.status)}"></herdr-status></div>
      <div class="hd-card__sub">model ${esc(s.model || '—')} · ${s.messages} msg · ${s.tool_calls} tool calls</div>
    </div>`).join('') + '</div>';
}

async function viewSession(parts) {
  const s = await client.getSession(parts[1]);
  if (!s) { root.innerHTML = '<herdr-error message="Session not found"></herdr-error>'; return; }
  root.innerHTML = `
    <h1>Session</h1>
    <div style="margin:8px 0"><herdr-status stage="${esc(s.status)}"></herdr-status>
      <span class="hd-card__sub"> · <herdr-ident kind="session" id="${esc(s.session_id)}"></herdr-ident></span></div>
    <div class="hd-grid">
      <herdr-metric label="Agent role" value="${esc(s.agent_role || '—')}"></herdr-metric>
      <herdr-metric label="Model" value="${esc(s.model || '—')}"></herdr-metric>
      <herdr-metric label="Messages" value="${s.messages}"></herdr-metric>
      <herdr-metric label="Tool calls" value="${s.tool_calls}"></herdr-metric>
    </div>
    <h3 style="margin-top:20px">Send a message</h3>
    <div class="hd-card">
      <form id="msg-form" class="hd-inline">
        <input id="msg-input" placeholder="steer the session…" style="flex:1;padding:8px 10px;border:1px solid var(--border);border-radius:var(--radius-sm)">
        <button type="submit" class="hd-btn">Send</button>
      </form>
    </div>
    <h3 style="margin-top:20px">Event stream</h3>
    <div id="session-stream">${await streamList('session', s.session_id)}</div>`;

  document.getElementById('msg-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const input = document.getElementById('msg-input');
    if (!input.value.trim()) return;
    try {
      await client.sendMessage(s.session_id, input.value.trim());
      input.value = '';
      document.getElementById('session-stream').innerHTML = await streamList('session', s.session_id);
    } catch (err) {
      alert(err.message);
    }
  });
}

async function viewServices() {
  const { services } = await client.listServices();
  if (!services.length) { root.innerHTML = '<herdr-empty message="No services registered"></herdr-empty>'; return; }
  root.innerHTML = '<h1>Services</h1><div class="hd-grid">' + services.map((svc) => `
    <div class="hd-card">
      <div class="hd-card__title"><a href="#/services/${esc(svc.service_id)}">${esc(svc.service_name)}</a></div>
      <div style="margin:6px 0"><herdr-status stage="${esc(svc.status)}"></herdr-status></div>
      <div class="hd-card__sub"><code>${esc(svc.url)}</code></div>
    </div>`).join('') + '</div>';
}

async function viewService(parts) {
  const svc = await client.request(`/api/v1/services/${esc(parts[1])}`);
  if (!svc || svc.error) { root.innerHTML = '<herdr-error message="Service not found"></herdr-error>'; return; }
  root.innerHTML = `
    <h1>${esc(svc.service_name)}</h1>
    <div style="margin:8px 0"><herdr-status stage="${esc(svc.status)}"></herdr-status></div>
    <div class="hd-grid">
      <herdr-metric label="Port" value="${esc(svc.port)}"></herdr-metric>
      <herdr-metric label="Host" value="${esc(svc.host_id)}"></herdr-metric>
    </div>
    <p>URL: <code>${esc(svc.url)}</code></p>`;
}

async function viewHosts() {
  const { hosts } = await client.listHosts();
  if (!hosts.length) { root.innerHTML = '<herdr-empty message="No hosts reporting"></herdr-empty>'; return; }
  root.innerHTML = '<h1>Hosts</h1><div class="hd-grid">' + hosts.map((h) => `
    <div class="hd-card">
      <div class="hd-card__title"><a href="#/hosts/${esc(h.host_id)}">${esc(h.host_name)}</a></div>
      <div style="margin:6px 0"><herdr-status stage="${esc(h.status)}"></herdr-status></div>
      <div class="hd-card__sub"><code>${esc(h.tailnet_ip)}</code> · seen ${formatTimestamp(h.last_seen)}</div>
    </div>`).join('') + '</div>';
}

async function viewHost(parts) {
  const h = await client.request(`/api/v1/hosts/${esc(parts[1])}`);
  if (!h || h.error) { root.innerHTML = '<herdr-error message="Host not found"></herdr-error>'; return; }
  root.innerHTML = `
    <h1>${esc(h.host_name)}</h1>
    <div style="margin:8px 0"><herdr-status stage="${esc(h.status)}"></herdr-status>
      <span class="hd-card__sub"> · <herdr-ident kind="host" id="${esc(h.host_id)}"></herdr-ident></span></div>
    <div class="hd-grid">
      <herdr-metric label="Tailnet IP" value="${esc(h.tailnet_ip)}"></herdr-metric>
      <herdr-metric label="CPU" value="${esc(h.cpu ?? '—')}"></herdr-metric>
      <herdr-metric label="Memory" value="${esc(h.mem ?? '—')}"></herdr-metric>
    </div>`;
}

async function viewActivity() {
  root.innerHTML = '<h1>Activity</h1>' + await activityList(50);
}

async function activityList(limit) {
  const { events } = await client.activity(limit);
  if (!events.length) return '<herdr-empty message="No activity yet"></herdr-empty>';
  return events.map((e) => `
    <div class="hd-card">
      <div class="hd-card__title">${esc(e.event_type)}</div>
      <div class="hd-card__sub">${esc(e.entity_type)} · <herdr-ident kind="event" id="${esc(e.event_id)}"></herdr-ident>
        · seq ${e.sequence} · ${formatTimestamp(e.source_timestamp)}</div>
      <code class="hd-card__sub">${esc(JSON.stringify(e.payload))}</code>
    </div>`).join('');
}

async function viewAnalytics() {
  const [missions, models, hosts] = await Promise.all([
    client.analytics('missions'), client.analytics('models'), client.analytics('hosts'),
  ]);
  root.innerHTML = `
    <h1>Analytics</h1>
    <div class="hd-grid">
      <herdr-metric label="Missions" value="${missions.total}"></herdr-metric>
      <herdr-metric label="Completion rate" value="${missions.completion_rate}%"></herdr-metric>
      <herdr-metric label="Models" value="${Object.keys(models.models).length}"></herdr-metric>
      <herdr-metric label="Hosts healthy" value="${hosts.healthy} / ${hosts.total}"></herdr-metric>
    </div>
    <h3 style="margin-top:20px">Missions by stage</h3>
    ${stageBars(missions.by_stage)}`;
}

async function viewAnalyticsMissions() { await viewAnalytics(); }
async function viewAnalyticsModels() {
  const { models, total_sessions } = await client.analytics('models');
  root.innerHTML = `<h1>Model analytics</h1><p>${total_sessions} total sessions</p><div class="hd-grid">` +
    Object.entries(models).map(([model, u]) => `
      <div class="hd-card">
        <div class="hd-card__title">${esc(model)}</div>
        <div class="hd-card__sub">${u.sessions} session(s) · ${u.messages} messages</div>
      </div>`).join('') + '</div>';
}
async function viewAnalyticsHosts() {
  const h = await client.analytics('hosts');
  root.innerHTML = `
    <h1>Host analytics</h1>
    <div class="hd-grid">
      <herdr-metric label="Total" value="${h.total}"></herdr-metric>
      <herdr-metric label="Healthy" value="${h.healthy}"></herdr-metric>
      <herdr-metric label="Unavailable" value="${h.unavailable}"></herdr-metric>
    </div>`;
}

function stageBars(byStage) {
  const entries = Object.entries(byStage || {});
  if (!entries.length) return '<herdr-empty message="No mission data"></herdr-empty>';
  return entries.map(([stage, n]) => `
    <div class="hd-card" style="display:flex;align-items:center;gap:12px">
      <herdr-status stage="${esc(stage)}"></herdr-status>
      <div style="flex:1;background:var(--surface-2);border-radius:4px;height:10px;overflow:hidden">
        <div style="width:${Math.min(100, n * 14)}%;height:100%;background:var(--accent)"></div>
      </div>
      <span class="hd-card__sub">${n}</span>
    </div>`).join('');
}

async function streamList(entityType, entityId) {
  const events = await client.request(`/api/v1/activity?limit=20`);
  const filtered = events.events.filter((e) => e.entity_type === entityType && e.entity_id === entityId);
  return filtered.length
    ? filtered.map((e) => `<div class="hd-card"><div class="hd-card__title">${esc(e.event_type)}</div>` +
        `<div class="hd-card__sub">seq ${e.sequence} · ${formatTimestamp(e.source_timestamp)}</div>` +
        `<code class="hd-card__sub">${esc(JSON.stringify(e.payload))}</code></div>`).join('')
    : '<herdr-empty message="No events for this entity yet"></herdr-empty>';
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

window.addEventListener('hashchange', render);
render();
