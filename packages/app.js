// HerdR Dev Fabric control-plane web app (spec 0190 §5, §20-34, §42-47).
// Hash-routed SPA. The UI is a *client* of the structured /api/v1 JSON + SSE
// stream. Views render loading / empty / error / data states (Spec §91).
import { HerdrClient } from './herdr-web-client/client.js';
import { connectEvents } from './herdr-web-client/events.js';
import { formatTimestamp } from './herdr-web-components/format.js';
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { defineComponents } from './herdr-web-components/index.js';
import { describeEvent, missionSubtitle, sessionSummary } from './herdr-web-components/humanize.js';

// Cache of { [mission_id]: title } so activity/session text can resolve
// cryptic mission ids to their human names. Fetched lazily, best-effort.
let missionTitleCache = null;
async function missionTitles() {
  if (missionTitleCache) return missionTitleCache;
  try {
    const { missions } = await client.listMissions();
    missionTitleCache = {};
    for (const m of missions) missionTitleCache[m.mission_id] = m.title;
  } catch { missionTitleCache = {}; }
  return missionTitleCache;
}
const titles = (cache) => cache || {};

defineComponents();

const client = new HerdrClient('');
const root = document.getElementById('view');

const NAV = [
  { path: '/overview', label: 'Overview' },
  { path: '/topology', label: 'Topology' },
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
  '/topology': viewTopology,
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
// ------------------------------------------------------------- topology
// Fleet tree: hosts -> sessions / services / CI runs / test adapters, with
// a recent-activity leaf. Reuses the existing /api/v1 data + CI/tests views.
function treeRow(label, sub, href, badge) {
  const link = href ? `<a href="${href}">${label}</a>` : esc(label);
  const b = badge ? `<span class="tp-badge">${esc(badge)}</span>` : '';
  return `<div class="tp-row">${link}${sub ? `<span class="tp-sub">${esc(sub)}</span>` : ''}${b}</div>`;
}

async function viewTopology() {
  const [hostsR, sessR, svcR, actR, ciR, testsR] = await Promise.all([
    client.listHosts(), client.listSessions(), client.listServices(),
    client.activity(30), client.getCi(), client.getTests(),
  ]);
  const hosts = hostsR.hosts || [];
  const sessions = sessR.sessions || [];
  const services = svcR.services || [];
  const events = actR.events || [];
  const ci = ciR || {};
  const tests = testsR || {};
  const t = titles(await missionTitles());

  const hostBlocks = hosts.map((h) => {
    const hs = sessions.filter((s) => s.host_id === h.host_id);
    const hsvc = services.filter((s) => s.host_id === h.host_id);
    const sRows = hs.length ? hs.slice(0, 12).map((s) =>
      treeRow(sessionSummary(s) || s.session_id, s.status,
              `#/herd/session/${esc(s.session_id)}`)).join('')
      : '<div class="tp-sub">no sessions</div>';
    const svRows = hsvc.length ? hsvc.slice(0, 12).map((s) =>
      treeRow(s.service_name, `${s.status} · ${s.route || ''}`,
              s.route ? s.route : undefined)).join('')
      : '';
    return `<details class="tp"><summary>${esc(h.host_name)}
      <span class="tp-sub">${esc(h.tailnet_ip || '')} · <herdr-status stage="${esc(h.status)}"></herdr-status></span></summary>
      <div class="tp-kids">
        <div class="tp-h">Sessions (${hs.length})</div>${sRows}
        ${svRows ? `<div class="tp-h">Services (${hsvc.length})</div>${svRows}` : ''}
      </div>
    </details>`;
  }).join('');

  const ciLeaves = ci.available ? Object.entries(ci.pipelines || {}).map(([repo, runs]) =>
    `<details class="tp"><summary>CI · ${esc(repo)}</summary><div class="tp-kids">` +
    (runs || []).slice(0, 8).map((r) =>
      treeRow(`${r.repository}#${r.run_number}`, `${r.state} · ${r.branch || ''}`,
              r.url, r.state)).join('') || '<div class="tp-sub">no runs</div>' +
    `</div></details>`).join('')
    : '<div class="tp-row">CI <span class="tp-sub">not configured / unavailable</span></div>';

  const testLeaves = (tests.adapters || []).map((a) =>
    treeRow(a, 'adapter')).join('');

  const actLeaves = events.slice(0, 8).map((e) => {
    const d = describeEvent(e, { missionTitles: t });
    return treeRow(d.title, `${d.kind} · ${formatTimestamp(e.source_timestamp)}`);
  }).join('') || '<div class="tp-sub">no activity</div>';

  root.innerHTML = `
    <style>
      .tp{margin:4px 0}.tp summary{cursor:pointer;padding:6px 8px;border-radius:6px;background:#1a2233}
      .tp summary:hover{background:#1f2b42}.tp-kids{margin-left:18px;padding-left:10px;border-left:1px solid #2a3550}
      .tp-h{color:#8a93a8;font-size:.78rem;text-transform:uppercase;margin:10px 0 4px}
      .tp-row{padding:3px 8px;border-radius:5px}.tp-row:hover{background:#1f2b42}
      .tp-sub{color:#8a93a8;font-size:.78rem;margin-left:8px}
      .tp-badge{float:right;font-size:.7rem;color:#4ade80}
    </style>
    <h1>Topology</h1>
    <div class="hd-card muted">${hosts.length} hosts · ${sessions.length} sessions · ${services.length} services · ${(ci.repos || []).length} CI repos · ${(tests.adapters || []).length} test adapters</div>
    <div class="tp-kids">${hostBlocks}
      <details class="tp" open><summary>CI runs</summary><div class="tp-kids">${ciLeaves}</div></details>
      <details class="tp" open><summary>Test adapters</summary><div class="tp-kids">${testLeaves}</div></details>
      <details class="tp" open><summary>Recent activity</summary><div class="tp-kids">${actLeaves}</div></details>
    </div>`;
}

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
      <div class="hd-card__sub">${esc(missionSubtitle(m, m.sessions ? m.sessions.length : 0))}</div>
      <div class="hd-card__sub">created ${formatTimestamp(m.created_at)}</div>
    </div>`).join('') + '</div>';
}

async function viewMission(parts) {
  const m = await client.getMission(parts[1]);
  if (!m) { root.innerHTML = '<herdr-error message="Mission not found"></herdr-error>'; return; }
  const sessions = m.sessions.length;
  const scope = m.purpose || m.cwd || m.title;
  root.innerHTML = `
    <h1>${esc(m.title)}</h1>
    <div style="margin:8px 0"><herdr-status stage="${esc(m.status)}"></herdr-status>
      <span class="hd-card__sub"> · <herdr-ident kind="mission" id="${esc(m.mission_id)}"></herdr-ident></span></div>
    <div class="hd-card" style="margin-top:10px">
      <div class="hd-card__sub">Scope</div>
      <div style="font-size:1.05em;margin-top:4px">${esc(scope || 'No scope recorded for this mission.')}</div>
      ${m.cwd && m.cwd !== scope ? `<div class="hd-card__sub" style="margin-top:8px"><code>${esc(m.cwd)}</code></div>` : ''}
    </div>
    <div class="hd-grid" style="margin-top:12px">
      <herdr-metric label="Sessions" value="${sessions}"></herdr-metric>
      <herdr-metric label="Stage" value="${esc(m.stage)}"></herdr-metric>
      <herdr-metric label="Created" value="${esc(formatTimestamp(m.created_at))}"></herdr-metric>
    </div>
    <h3 style="margin-top:20px">Sessions</h3>
    ${await missionSessions(m)}
    <h3 style="margin-top:20px">Event stream</h3>
    <div id="mission-stream">${await streamList('mission', m.mission_id)}</div>`;
}

async function missionSessions(m) {
  const list = m.sessions || [];
  if (!list.length) return '<herdr-empty message="No sessions on this mission yet"></herdr-empty>';
  const t = titles(await missionTitles());
  return list.map((s) => `
    <div class="hd-card">
      <div class="hd-card__title"><a href="#/herd/session/${esc(s.session_id)}">${esc(s.agent_role || 'agent session')}</a></div>
      <div style="margin:6px 0"><herdr-status stage="${esc(s.status)}"></herdr-status></div>
      <div class="hd-card__sub">${esc(sessionSummary(s, t))}</div>
    </div>`).join('');
}

async function viewSessions() {
  const { sessions } = await client.listSessions();
  if (!sessions.length) { root.innerHTML = '<herdr-empty message="No sessions yet"></herdr-empty>'; return; }
  const t = titles(await missionTitles());
  root.innerHTML = '<h1>Sessions</h1><div class="hd-grid">' + sessions.map((s) => `
    <div class="hd-card">
      <div class="hd-card__title"><a href="#/herd/session/${esc(s.session_id)}">${esc(s.agent_role || 'agent session')}</a></div>
      <div style="margin:6px 0"><herdr-status stage="${esc(s.status)}"></herdr-status></div>
      <div class="hd-card__sub">${esc(sessionSummary(s, t))}</div>
    </div>`).join('') + '</div>';
}

async function viewSession(parts) {
  const s = await client.getSession(parts[1]);
  if (!s) { root.innerHTML = '<herdr-error message="Session not found"></herdr-error>'; return; }
  const t = titles(await missionTitles());
  const missionTitle = s.mission_id ? (t[s.mission_id] || s.mission_id) : '—';
  const attach = s.attach || (s.host_id && s.mission_id
    ? `ssh ${s.host_id} && herdr workspace focus ${s.mission_id}` : '');
  const rows = [
    ['What it is doing', s.summary],
    ['Directory (cwd)', s.cwd],
    ['Git branch', s.branch],
    ['PID', s.pid],
    ['Command line', s.cmdline],
  ].filter(([, v]) => v);
  root.innerHTML = `
    <h1>${esc(s.agent_role || 'Agent session')}</h1>
    <div style="margin:8px 0"><herdr-status stage="${esc(s.status)}"></herdr-status>
      <span class="hd-card__sub"> · <herdr-ident kind="session" id="${esc(s.session_id)}"></herdr-ident></span></div>
    <div class="hd-grid">
      <herdr-metric label="Agent role" value="${esc(s.agent_role || '—')}"></herdr-metric>
      <herdr-metric label="Model" value="${esc(s.model || '—')}"></herdr-metric>
      <herdr-metric label="Mission" value="${esc(missionTitle)}"></herdr-metric>
      <herdr-metric label="Host" value="${esc(s.host_id || '—')}"></herdr-metric>
    </div>
    ${rows.length ? `<div class="hd-card" style="margin-top:12px">
      <div class="hd-card__sub">Operational detail</div>
      ${rows.map(([k, v]) => `
        <div style="margin-top:8px">
          <div class="hd-card__sub">${esc(k)}</div>
          <div><code>${esc(v)}</code></div>
        </div>`).join('')}
      ${attach ? `<div style="margin-top:10px">
        <div class="hd-card__sub">Attach</div>
        <div class="hd-inline">
          <code style="flex:1">${esc(attach)}</code>
          <button class="hd-btn" id="copy-attach">Copy</button>
        </div>
      </div>` : ''}
    </div>` : ''}
    <h3 style="margin-top:20px">Send a message</h3>
    <div class="hd-card">
      <form id="msg-form" class="hd-inline">
        <input id="msg-input" placeholder="steer the session…" style="flex:1;padding:8px 10px;border:1px solid var(--border);border-radius:var(--radius-sm)">
        <button type="submit" class="hd-btn">Send</button>
      </form>
    </div>
    <h3 style="margin-top:20px">Event stream</h3>
    <div id="session-stream">${await streamList('session', s.session_id)}</div>`;

  const copyBtn = document.getElementById('copy-attach');
  if (copyBtn) {
    copyBtn.addEventListener('click', () => copyText(attach, copyBtn));
  }
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

async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
  }
  const original = btn.textContent;
  btn.textContent = 'Copied';
  setTimeout(() => { btn.textContent = original; }, 1200);
}

async function viewServices() {
  const { services } = await client.listServices();
  if (!services.length) { root.innerHTML = '<herdr-empty message="No services registered"></herdr-empty>'; return; }
  root.innerHTML = '<h1>Services</h1><div class="hd-grid">' + services.map((svc) => `
    <div class="hd-card">
      <div class="hd-card__title"><a href="#/services/${esc(svc.service_id)}">${esc(svc.service_name)}</a></div>
      <div style="margin:6px 0"><herdr-status stage="${esc(svc.status)}"></herdr-status></div>
      <div class="hd-card__sub"><a href="${esc(svc.url)}" target="_blank" rel="noopener">${esc(svc.url)}</a></div>
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
    <p>URL: <a href="${esc(svc.url)}" target="_blank" rel="noopener">${esc(svc.url)}</a></p>`;
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
  let cpu = '—', mem = '—', disk = '—', seen = '—';
  try {
    const { samples } = await client.request(`/api/v1/hosts/${esc(parts[1])}/telemetry?limit=1`);
    if (samples && samples.length) {
      const s = samples[0];
      cpu = (s.cpu ?? s.cpu_pct) ?? '—';
      mem = (s.mem ?? s.mem_pct) ?? '—';
      disk = s.disk_pct ?? '—';
      seen = formatTimestamp(s.at ?? h.last_seen);
    }
  } catch { /* telemetry optional */ }
  root.innerHTML = `
    <h1>${esc(h.host_name)}</h1>
    <div style="margin:8px 0"><herdr-status stage="${esc(h.status)}"></herdr-status>
      <span class="hd-card__sub"> · <herdr-ident kind="host" id="${esc(h.host_id)}"></herdr-ident></span></div>
    <div class="hd-grid">
      <herdr-metric label="Tailnet IP" value="${esc(h.tailnet_ip)}"></herdr-metric>
      <herdr-metric label="CPU" value="${esc(cpu)}"></herdr-metric>
      <herdr-metric label="Memory" value="${esc(mem)}"></herdr-metric>
      <herdr-metric label="Disk" value="${esc(disk)}"></herdr-metric>
    </div>
    <p class="hd-card__sub">last telemetry ${esc(seen)}</p>`;
}

async function viewActivity() {
  root.innerHTML = '<h1>Activity</h1>' + await activityList(50);
}

async function activityList(limit) {
  const { events } = await client.activity(limit);
  if (!events.length) return '<herdr-empty message="No activity yet"></herdr-empty>';
  const t = titles(await missionTitles());
  return events.map((e) => {
    const d = describeEvent(e, { missionTitles: t });
    const meta = `<div class="hd-card__sub">${esc(d.kind)} · ${formatTimestamp(e.source_timestamp)}</div>`;
    if (d.muted) {
      // Low-signal events (e.g. heartbeats) get a compact, de-emphasised row.
      return `<div class="hd-card" style="opacity:.6">
        <div class="hd-card__title">${esc(d.title)}</div>
        <div class="hd-card__sub">${esc(d.detail)}</div>${meta}</div>`;
    }
    return `<div class="hd-card">
      <div class="hd-card__title">${esc(d.title)}</div>
      <div class="hd-card__sub">${esc(d.detail)}</div>${meta}
    </div>`;
  }).join('');
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
  const { events } = await client.request(
    `/api/v1/events?entity_type=${entityType}&entity_id=${entityId}`);
  if (!events.length) return '<herdr-empty message="No events for this entity yet"></herdr-empty>';
  const t = titles(await missionTitles());
  return events.map((e) => {
    const d = describeEvent(e, { missionTitles: t });
    return `<div class="hd-card">
      <div class="hd-card__title">${esc(d.title)}</div>
      <div class="hd-card__sub">${esc(d.detail)}</div>
      <div class="hd-card__sub">seq ${e.sequence} · ${formatTimestamp(e.source_timestamp)}</div>
    </div>`;
  }).join('');
}

// Live updates (Spec §52, §85): subscribe to the SSE stream and refresh the
// current view when new events arrive. A full re-render re-fetches all data
// for the view, so refreshing on *every* event (the fleet emits several per
// second) would trip the API's per-IP rate limit ("too many requests") and
// exhaust the browser's HTTP/1.1 connection pool ("Failed to fetch"). We
// therefore throttle the SSE-driven refresh to at most once per
// LIVE_REFRESH_MS (trailing edge). User navigation still renders immediately.
let liveTimer = null;
let lastLiveRender = 0;
const LIVE_REFRESH_MS = 5000;
connectEvents({
  url: '/api/v1/events/stream',
  onEvent: () => {
    if (liveTimer) clearTimeout(liveTimer);
    // Fire shortly after the last event, but never more often than once per
    // LIVE_REFRESH_MS since the previous live refresh.
    const wait = Math.max(150, LIVE_REFRESH_MS - (Date.now() - lastLiveRender));
    liveTimer = setTimeout(() => {
      lastLiveRender = Date.now();
      render();
    }, wait);
  },
  onError: () => { /* keep last view; the client auto-reconnects */ },
});

window.addEventListener('hashchange', () => { lastLiveRender = Date.now(); render(); });
render();
