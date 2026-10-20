// HerdR Dev Fabric control-plane web app (spec 0190 §5, §20-34, §42-47).
// Hash-routed SPA. The UI is a *client* of the structured /api/v1 JSON + SSE
// stream. Views render loading / empty / error / data states (Spec §91).
import { HerdrClient } from './herdr-web-client/client.js';
import { connectEvents } from './herdr-web-client/events.js';
import { formatTimestamp } from './herdr-web-components/format.js';
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { defineComponents } from './herdr-web-components/index.js';
import { describeEvent, missionSubtitle, sessionSummary } from './herdr-web-components/humanize.js';
import { statusForStage, statusLabel, tokenVar } from './herdr-web-components/status.js';
import { mountInspector } from './inspector-view.js';
import { mountMissionActivity } from './mission-live-view.js';
import { mountServices, renderServiceDetails } from './services-view.js';

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

// Non-herdr-sourced missions are keyed by mission_key (e.g. 'pi:MSN-1'); the
// bare mission_id only resolves for herdr-sourced ones (UI-005, UI-008).
function missionKey(m) { return m.mission_key || m.mission_id; }

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
  '/analytics/tokens': viewAnalyticsTokens,
};

// The agent inspector owns a per-session stream; dispose it whenever the view changes.
let disposeInspector = null;
let disposeMissionActivity = null;
let refreshMissionHistory = null;
let servicesView = null;
let renderRevision = 0;
const onMissionPage = () => /^#\/missions(?:\/|$)/.test(window.location.hash);
const onSessionPage = () => /^#\/herd\/session\//.test(window.location.hash);

async function render() {
  const revision = ++renderRevision;
  refreshMissionHistory = null;
  if (servicesView) { servicesView.dispose(); servicesView = null; }
  if (disposeMissionActivity) { disposeMissionActivity(); disposeMissionActivity = null; }
  if (disposeInspector) { disposeInspector(); disposeInspector = null; }
  const { parts } = route();
  const seg = parts.length ? parts.join('/') : 'overview';
  const match = Object.keys(ROUTES).find((r) => {
    const rp = r.split('/').filter(Boolean);
    if (rp.length !== parts.length) return false;
    return rp.every((rpseg, i) => rpseg.startsWith(':') || rpseg === parts[i]);
  });
  root.innerHTML = '<herdr-spinner label="Loading"></herdr-spinner>';
  try {
    await ROUTES[match || '/overview'](parts, revision);
  } catch (err) {
    if (revision !== renderRevision) return;
    root.innerHTML = `<herdr-error message="${esc(err.message || 'request failed')}"></herdr-error>`;
  }
}

// ------------------------------------------------------------- views
// ------------------------------------------------------------- topology
// Fleet tree: hosts -> sessions / services / CI runs / test adapters, with
// a recent-activity leaf. Reuses the existing /api/v1 data + CI/tests views.
// Status dots/badges reuse the theme's semantic tokens (theme.css + status.js)
// so the tree stays consistent with the rest of the control plane.
function treeRow(label, sub, href, status, accent) {
  const link = href ? `<a href="${href}">${label}</a>` : esc(label);
  const st = status ? statusForStage(status) : null;
  const dotColor = st ? tokenVar(st.token) : (accent || 'var(--text-muted)');
  const dot = `<span class="tp-dot" style="background:${dotColor}"></span>`;
  const b = st
    ? `<span class="tp-badge" style="color:${tokenVar(st.token)};border-color:${tokenVar(st.token)}">${esc(statusLabel(status))}</span>`
    : '';
  const subHtml = sub ? `<span class="tp-sub">${esc(sub)}</span>` : '';
  return `<div class="tp-row">${dot}${link}${subHtml}${b}</div>`;
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
      treeRow(sessionSummary(s) || s.session_id, '',
              `#/herd/session/${esc(s.session_id)}`, s.status,
              'var(--status-active)')).join('')
      : '<div class="tp-hint">no sessions</div>';
    const svRows = hsvc.length ? hsvc.slice(0, 12).map((s) =>
      treeRow(s.service_name, s.route || '', s.route ? s.route : undefined,
              s.status, 'var(--accent)')).join('')
      : '';
    return `<details class="tp tp-host"><summary>
        ${esc(h.host_name)}
        <span class="tp-sub">${esc(h.tailnet_ip || '')} · <herdr-status stage="${esc(h.status)}"></herdr-status></span>
      </summary>
      <div class="tp-kids">
        <div class="tp-h">Sessions (${hs.length})</div>${sRows}
        ${svRows ? `<div class="tp-h">Services (${hsvc.length})</div>${svRows}` : ''}
      </div>
    </details>`;
  }).join('');

  const ciLeaves = ci.available
    ? Object.entries(ci.pipelines || {}).map(([repo, runs]) =>
        `<details class="tp tp-ci"><summary>${esc(repo)}</summary><div class="tp-kids">` +
        ((runs || []).length ? (runs || []).slice(0, 8).map((r) =>
            treeRow(`#${r.run_number}`, r.branch || '', r.url, r.state,
                    'var(--status-pr)')).join('')
          : '<div class="tp-hint">no runs</div>') +
        `</div></details>`).join('')
    : '<div class="tp-row"><span class="tp-dot" style="background:var(--text-muted)"></span>CI <span class="tp-sub">not configured / unavailable</span></div>';

  const testLeaves = (tests.adapters || []).length
    ? (tests.adapters || []).map((a) =>
        treeRow(a, 'adapter', undefined, undefined, 'var(--status-testing)')).join('')
    : '<div class="tp-hint">no adapters discovered</div>';

  const actLeaves = events.slice(0, 8).map((e) => {
    const d = describeEvent(e, { missionTitles: t });
    return treeRow(d.title, `${d.kind} · ${formatTimestamp(e.source_timestamp)}`,
                    undefined, undefined, 'var(--text-muted)');
  }).join('') || '<div class="tp-hint">no activity</div>';

  root.innerHTML = `
    <style>
      .tp{margin:2px 0}
      .tp summary{display:flex;align-items:center;gap:8px;cursor:pointer;list-style:none;
        padding:7px 10px;border-radius:var(--radius);background:var(--surface);
        border:1px solid var(--border-subtle);color:var(--text);font-weight:600;
        font-size:.86rem;transition:background var(--motion-fast),border-color var(--motion-fast)}
      .tp summary::-webkit-details-marker{display:none}
      .tp summary::before{content:'';width:8px;height:8px;border-radius:2px;flex:none;
        border:1px solid var(--text-muted);opacity:.5;transition:transform var(--motion-fast)}
      .tp summary:hover{background:var(--surface-hover);border-color:var(--border)}
      .tp[open] summary::before{background:var(--tp-accent,var(--accent));
        border-color:var(--tp-accent,var(--accent));transform:rotate(45deg)}
      .tp-host summary{--tp-accent:var(--status-planning)}
      .tp-ci summary{--tp-accent:var(--status-pr)}
      .tp-tests summary{--tp-accent:var(--status-testing)}
      .tp-act summary{--tp-accent:var(--text-muted)}
      .tp-kids{margin-left:13px;padding-left:13px;border-left:1px solid var(--border);margin-top:2px}
      .tp-h{color:var(--text-muted);font-size:.68rem;font-weight:700;letter-spacing:.07em;
        text-transform:uppercase;margin:12px 0 4px}
      .tp-hint{color:var(--text-muted);font-size:.76rem;padding:3px 8px}
      .tp-row{display:flex;align-items:center;gap:8px;padding:4px 8px;border-radius:var(--radius-sm);
        color:var(--text);font-size:.82rem}
      .tp-row a{color:inherit;text-decoration:none}
      .tp-row:hover{background:var(--surface-hover)}
      .tp-row:hover a{color:var(--accent);text-decoration:underline}
      .tp-dot{width:7px;height:7px;border-radius:50%;flex:none}
      .tp-sub{color:var(--text-muted);font-size:.74rem;margin-left:auto;text-align:right;
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:55%}
      .tp-badge{flex:none;font-size:.62rem;font-weight:700;padding:1px 7px;border-radius:99px;
        background:color-mix(in srgb, currentColor 9%, var(--surface));border:1px solid}
      .tp-count{color:var(--text-muted);font-weight:600;font-size:.74rem;margin-left:auto}
    </style>
    <h1>Topology</h1>
    <div class="hd-card muted">${hosts.length} hosts · ${sessions.length} sessions · ${services.length} services · ${(ci.repos || []).length} CI repos · ${(tests.adapters || []).length} test adapters</div>
    <div class="tp-kids">${hostBlocks}
      <details class="tp tp-ci" open><summary>CI runs<span class="tp-count">${(ci.repos || []).length || ''}</span></summary><div class="tp-kids">${ciLeaves}</div></details>
      <details class="tp tp-tests" open><summary>Test adapters<span class="tp-count">${(tests.adapters || []).length || ''}</span></summary><div class="tp-kids">${testLeaves}</div></details>
      <details class="tp tp-act" open><summary>Recent activity<span class="tp-count">${events.length || ''}</span></summary><div class="tp-kids">${actLeaves}</div></details>
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
  root.innerHTML = '<h1>Missions</h1><h2>Live Pi missions</h2><div id="live-missions"></div><h2>Recorded missions</h2><div id="recorded-missions"><herdr-spinner label="Loading recorded missions"></herdr-spinner></div>';
  disposeMissionActivity = mountMissionActivity(document.getElementById('live-missions'), client);
  const recorded = document.getElementById('recorded-missions');
  let pending = false;
  refreshMissionHistory = async () => {
    if (pending || !recorded.isConnected || document.hidden) return;
    pending = true;
    try {
      const { missions } = await client.listMissions();
      if (!recorded.isConnected) return;
      recorded.innerHTML = missions.length ? '<div class="hd-grid">' + missions.map((m) => `
        <div class="hd-card">
          <div class="hd-card__title"><a href="#/missions/${encodeURIComponent(missionKey(m))}">${esc(m.title)}</a></div>
          <div style="margin:6px 0"><herdr-status stage="${esc(m.status)}"></herdr-status></div>
          <div class="hd-card__sub">${esc(missionSubtitle(m, m.sessions ? m.sessions.length : 0))}</div>
          <div class="hd-card__sub">created ${formatTimestamp(m.created_at)}</div>
          <herdr-mission-activity updated-at="${esc(m.updated_at || '')}" source="${esc(m.source || '')}"></herdr-mission-activity>
        </div>`).join('') + '</div>' : '<herdr-empty message="No recorded missions yet"></herdr-empty>';
    } catch (error) {
      if (recorded.isConnected) recorded.innerHTML = `<herdr-error message="${esc(error.message)}"></herdr-error>`;
    } finally { pending = false; }
  };
  await refreshMissionHistory();
}

async function viewMission(parts, revision) {
  const key = decodeURIComponent(parts[1]);
  let m;
  try { m = await client.getMission(key); }
  catch (error) { if (error.status !== 404 || !key.startsWith('pi:')) throw error; }
  if (revision !== renderRevision) return;
  if (key.startsWith('pi:') && !m) {
    root.innerHTML = '<h1>Pi mission activity</h1><div id="live-mission-detail"></div>';
    disposeMissionActivity = mountMissionActivity(document.getElementById('live-mission-detail'), client, key);
    return;
  }
  if (!m) { root.innerHTML = '<herdr-error message="Mission not found"></herdr-error>'; return; }
  root.innerHTML = `<h1>${esc(m.title)}</h1>${key.startsWith('pi:') ? '<div id="live-mission-detail"></div>' : ''}<div id="mission-history"></div>`;
  if (key.startsWith('pi:')) disposeMissionActivity = mountMissionActivity(document.getElementById('live-mission-detail'), client, key);
  const history = document.getElementById('mission-history');
  let pending = false;
  const refreshHistory = async (initial) => {
    if (pending || !history.isConnected || document.hidden) return;
    pending = true;
    try {
      const m = initial || await client.getMission(key);
      const sessions = (m.sessions || []).length;
      const scope = m.purpose || m.cwd || m.title;
      const [sessionsHtml, streamHtml] = await Promise.all([missionSessions(m), streamList('mission', missionKey(m))]);
      if (revision !== renderRevision || !history.isConnected) return;
      history.innerHTML = `<h2>Recorded mission</h2>
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
    <herdr-mission-activity updated-at="${esc(m.updated_at || '')}" source="${esc(m.source || '')}"></herdr-mission-activity>
    <h3 style="margin-top:20px">Sessions</h3>
    ${sessionsHtml}
    <h3 style="margin-top:20px">Timeline</h3>
    <herdr-timeline id="mission-timeline"></herdr-timeline>
    <h3 style="margin-top:20px">Event stream</h3>
    <div id="mission-stream">${streamHtml}</div>`;

      const timelineElement = history.querySelector('#mission-timeline');
      try {
        const { timeline } = await client.missionTimeline(missionKey(m));
        timelineElement.items = timeline || [];
      } catch { timelineElement.items = []; }
    } catch (error) {
      if (history.isConnected) history.innerHTML = `<herdr-error message="${esc(error.message)}"></herdr-error>`;
    } finally { pending = false; }
  };
  if (key.startsWith('pi:')) refreshMissionHistory = refreshHistory;
  await refreshHistory(m);
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
  const s = await client.getSession(parts[parts.length - 1]);
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
    <div id="inspector-host"></div>
    <h3 style="margin-top:20px">Event stream</h3>
    <div id="session-stream">${await streamList('session', s.session_id)}</div>`;
  disposeInspector = mountInspector(document.getElementById('inspector-host'), client, s.session_id);

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

async function viewServices(_parts, revision) {
  const { services } = await client.listServices();
  if (revision !== renderRevision) return;
  servicesView = mountServices(root, client, services, { showAll: route().query.get('all') === '1' });
}

async function viewService(parts, revision) {
  const svc = await client.request(`/api/v1/services/${encodeURIComponent(decodeURIComponent(parts[1]))}`);
  if (revision !== renderRevision) return;
  if (!svc || svc.error) { root.innerHTML = '<herdr-error message="Service not found"></herdr-error>'; return; }
  root.innerHTML = renderServiceDetails(svc);
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
    <p style="margin-top:12px"><a href="#/analytics/tokens">Token usage by project &rarr;</a></p>
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
async function viewAnalyticsTokens() {
  const t = await client.analytics('tokens');
  const fmt = (n) => Number(n || 0).toLocaleString();
  const rows = (bucket) => Object.entries(bucket).map(([k, u]) => `
      <tr><td>${esc(k)}</td><td>${fmt(u.requests)}</td><td>${fmt(u.input_tokens)}</td>
      <td>${fmt(u.output_tokens)}</td><td>${fmt(u.reasoning_tokens)}</td><td>${fmt(u.failed)}</td></tr>`).join('');
  const table = (title, bucket) => `<h3 style="margin-top:20px">${title}</h3>
    <table class="hd-table"><thead><tr><th></th><th>Requests</th><th>Input</th><th>Output</th>
    <th>Reasoning</th><th>Failed</th></tr></thead><tbody>${rows(bucket) ||
      '<tr><td colspan="6">No usage recorded yet.</td></tr>'}</tbody></table>`;
  root.innerHTML = `<h1>Token usage</h1>
    <div class="hd-grid">
      <herdr-metric label="Requests" value="${fmt(t.total.requests)}"></herdr-metric>
      <herdr-metric label="Input tokens" value="${fmt(t.total.input_tokens)}"></herdr-metric>
      <herdr-metric label="Output tokens" value="${fmt(t.total.output_tokens)}"></herdr-metric>
    </div>` + table('By project', t.by_project) + table('By model', t.by_model) +
    table('By day', t.by_day);
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
    if (servicesView) {
      if (!liveTimer) liveTimer = setTimeout(() => { liveTimer = null; servicesView?.refresh(); }, LIVE_REFRESH_MS);
      return;
    }
    // Mission telemetry owns its polling; SSE must not reset worker inspection.
    if (onMissionPage() && (disposeMissionActivity || decodeURIComponent(route().parts[1] || '').startsWith('pi:'))) {
      // Refresh recorded lifecycle data separately so disclosures stay open.
      if (refreshMissionHistory && !liveTimer) liveTimer = setTimeout(() => {
        liveTimer = null;
        refreshMissionHistory?.();
      }, LIVE_REFRESH_MS);
      return;
    }
    // The session page streams its own detail; a full re-render would reset it.
    // Only the (cheap) generic event list below the inspector is refreshed there.
    if (onSessionPage()) {
      const el = document.getElementById('session-stream');
      if (el && !liveTimer) {
        const sid = decodeURIComponent(window.location.hash.split('/').pop());
        liveTimer = setTimeout(async () => {
          liveTimer = null;
          if (!onSessionPage()) return;
          try { el.innerHTML = await streamList('session', sid); } catch { /* keep last */ }
        }, LIVE_REFRESH_MS);
      }
      return;
    }
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

window.addEventListener('hashchange', () => {
  if (liveTimer) { clearTimeout(liveTimer); liveTimer = null; }
  lastLiveRender = Date.now(); render();
});
render();
