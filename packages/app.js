// HerdR Dev Fabric control-plane web app (spec 0190 §5, §20-34, §42-47).
// Hash-routed SPA. The UI is a *client* of the structured /api/v1 JSON + SSE
// stream. Views render loading / empty / error / data states (Spec §91).
import { HerdrClient } from './herdr-web-client/client.js';
import { connectEvents } from './herdr-web-client/events.js';
import { formatTimestamp } from './herdr-web-components/format.js';
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { defineComponents } from './herdr-web-components/index.js';
import { describeEvent, missionSubtitle, sessionSummary } from './herdr-web-components/humanize.js';
import { mountInspector } from './inspector-view.js';
import { mountMissionActivity } from './mission-live-view.js';
import { mountMissionCatalog } from './mission-catalog-view.js';
import { mountOverview } from './overview-view.js';
import { mountIntegrations } from './integrations-view.js';
import { mountGithub } from './github-view.js';
import { mountTopology } from './topology-view.js';
import { mountSessionsView, sessionEntries } from './sessions-view.js';
import { recordOutcome, statusOutcome } from './record-state.js';
import { mountServices, mountServiceDetails } from './services-view.js';

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
  { path: '/system-services', label: 'System services' },
  { path: '/github', label: 'Projects' },
  { path: '/ci', label: 'CI' },
  { path: '/reviews', label: 'Reviews' },
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
  '/github': viewGithub,
  '/ci': viewIntegrations,
  '/reviews': viewIntegrations,
  '/topology': viewTopology,
  '/missions': viewMissions,
  '/missions/:id': viewMission,
  '/herd': viewSessions,
  '/herd/session/:id': viewSession,
  '/services': viewServices,
  '/system-services': viewServices,
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
let dashboardView = null;
let sessionRequest = null;
let renderRevision = 0;
const onMissionPage = () => route().parts[0] === 'missions';
const onSessionPage = () => route().parts[0] === 'herd' && route().parts[1] === 'session';

async function render() {
  const revision = ++renderRevision;
  refreshMissionHistory = null;
  if (dashboardView) { dashboardView.dispose(); dashboardView = null; }
  if (sessionRequest) { sessionRequest.abort(); sessionRequest = null; }
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
async function viewTopology() {
  dashboardView = mountTopology(root, client, {status: route().query.get('status') || 'current'});
}

async function viewOverview() {
  root.innerHTML = '<h1>Overview</h1><div id="overview-dashboard"></div>';
  dashboardView = mountOverview(document.getElementById('overview-dashboard'), client);
}

async function viewGithub() {
  dashboardView = mountGithub(root, client);
}

async function viewIntegrations(parts) {
  const query = route().query;
  dashboardView = mountIntegrations(root, client, {
    kind: parts[0], tab: query.get('tab') || 'current', search: query.get('search') || '',
    repository: query.get('repository') || '', mission: query.get('mission') || '', session: query.get('session') || '',
  });
}

async function viewMissions() {
  root.innerHTML = '<h1>Missions</h1><div id="live-missions"></div>';
  const catalog = mountMissionCatalog(document.getElementById('live-missions'), client, {tab: route().query.get('tab') || 'current', status: route().query.get('status') || 'all'});
  disposeMissionActivity = catalog.dispose;
  refreshMissionHistory = catalog.refreshRecorded;
}

function relatedIntegrationLinks(record, kind, id) {
  const context = new URLSearchParams({[kind]: id});
  const repository = typeof record.repository === 'string' ? record.repository : '';
  const revision = typeof record.revision === 'string' ? record.revision : '';
  const ci = new URLSearchParams();
  if (repository) ci.set('repository', repository);
  if (revision) ci.set('search', revision);
  return `<nav class="hd-inline" aria-label="Related reviews and builds" style="margin:14px 0;font-size:13px"><a href="#/reviews?${esc(context)}">Related reviews</a><a href="#/ci?${esc(context)}">Related CI</a>${ci.size ? `<a href="#/ci?${esc(ci)}">CI for this ${revision ? 'revision' : 'repository'}</a>` : ''}</nav>`;
}

async function viewMission(parts, revision) {
  const key = decodeURIComponent(parts[1]);
  let m;
  try { m = await client.getMission(key); }
  catch (error) { if (error.status !== 404 || !key.startsWith('pi:')) throw error; }
  if (revision !== renderRevision) return;
  if (key.startsWith('pi:') && !m) {
    root.innerHTML = `<a href="#/missions">← Missions</a><h1>Pi mission</h1>${relatedIntegrationLinks({},'mission',key)}<div id="live-mission-detail"></div>`;
    disposeMissionActivity = mountMissionActivity(document.getElementById('live-mission-detail'), client, key);
    return;
  }
  if (!m) { root.innerHTML = '<herdr-error message="Mission not found"></herdr-error>'; return; }
  root.innerHTML = `<a href="#/missions">← Missions</a><h1 style="overflow-wrap:anywhere">${key.startsWith('pi:') ? 'Pi mission' : esc((m.title || 'Mission').slice(0, 180))}</h1>${key.startsWith('pi:') ? '<div id="live-mission-detail"></div>' : ''}${relatedIntegrationLinks(m,'mission',key)}<div id="mission-history"></div>`;
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
      const open = new Map([...history.querySelectorAll('details[data-history-panel]')].map(el => [el.dataset.historyPanel, el.open]));
      history.innerHTML = `<h2>Recorded history</h2>
    <div style="margin:8px 0"><herdr-status stage="${esc(m.status)}"></herdr-status>
      <span class="hd-card__sub"> · ${sessions} sessions · ${esc(m.stage || 'Stage not recorded')} · created ${esc(formatTimestamp(m.created_at))}</span></div>
    <herdr-mission-activity updated-at="${esc(m.updated_at || '')}" source="${esc(m.source || '')}"></herdr-mission-activity>
    <details class="hd-card" data-history-panel="brief"><summary>Recorded mission brief</summary>
      <p style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(m.title || '')}</p>
      <div class="hd-card__sub">Scope</div><p style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(scope || 'No scope recorded for this mission.')}</p>
      ${m.cwd && m.cwd !== scope ? `<p style="overflow-wrap:anywhere"><code>${esc(m.cwd)}</code></p>` : ''}
      <herdr-ident kind="mission" id="${esc(m.mission_id)}"></herdr-ident>
    </details>
    <details class="hd-card" data-history-panel="sessions"><summary>Recorded sessions · ${sessions}</summary>${sessionsHtml}</details>
    <details class="hd-card" data-history-panel="timeline"><summary>Timeline</summary><herdr-timeline id="mission-timeline"></herdr-timeline></details>
    <details class="hd-card" data-history-panel="events"><summary>Event stream</summary><div id="mission-stream">${streamHtml}</div></details>`;
      history.querySelectorAll('details[data-history-panel]').forEach(el => { if (open.has(el.dataset.historyPanel)) el.open = open.get(el.dataset.historyPanel); });

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
      <div class="hd-card__title"><a href="#/herd/session/${esc(encodeURIComponent(s.session_id))}">${esc(s.agent_role || 'agent session')}</a></div>
      <div style="margin:6px 0"><herdr-status stage="${esc(s.status)}"></herdr-status></div>
      <div class="hd-card__sub">${esc(sessionSummary(s, t))}</div>
    </div>`).join('');
}

async function viewSessions() {
  dashboardView = mountSessionsView(root, client, {tab: route().query.get('tab') || 'current'});
}

async function viewSession(parts, revision) {
  const id = decodeURIComponent(parts[parts.length - 1]);
  const controller = new AbortController();
  sessionRequest = controller;
  const deadline = setTimeout(() => controller.abort(), 15000);
  try {
    const s = await client.getSession(id, {signal: controller.signal});
    if (revision !== renderRevision || controller.signal.aborted) return;
    if (!s) { root.innerHTML = '<herdr-error message="Session not found"></herdr-error>'; return; }
    const outcome = recordOutcome(s);
    const terminal = outcome === 'failed' || outcome === 'finished';
    const reportedStatus = terminal && statusOutcome(s.status) !== outcome ? s.stage : s.status;
    const clip = value => typeof value === 'string' ? value.slice(0, 180) : '';
    const project = typeof s.cwd === 'string' ? s.cwd.replace(/\\/g, '/').split('/').filter(Boolean).pop() : '';
    const attach = typeof s.attach === 'string' ? s.attach : '';
    const fields = [['Session ID',s.session_id],['Last reported status',s.status],['Stage',s.stage],['Directory',s.cwd],['Git branch',s.branch],['PID',s.pid],['Command line',s.cmdline],['Model',s.model],['Last reported',formatTimestamp(s.updated_at)]].filter(([,v])=>v != null && v !== '');
    root.innerHTML = `<style>.sd-head{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:14px 0}.sd-meta{color:var(--text-secondary);line-height:1.6;overflow-wrap:anywhere}.sd-details{margin:14px 0;overflow-wrap:anywhere}.sd-details summary{cursor:pointer;font-weight:600}.sd-details dl{display:grid;grid-template-columns:minmax(100px,auto) minmax(0,1fr);gap:8px 16px}.sd-details dt{color:var(--text-secondary)}.sd-details dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.sd-title{overflow-wrap:anywhere}@media(max-width:600px){.sd-details dl{display:block}.sd-details dd{margin:4px 0 12px}}</style>
      <a href="#/herd?tab=${terminal ? outcome : 'current'}">← Sessions</a>
      <h1 class="sd-title">${esc(clip(s.agent_role) || 'Agent session')}${project ? ` · ${esc(clip(project))}` : ''}</h1>
      <div class="sd-head"><strong>${esc(reportedStatus ? `Last reported ${String(reportedStatus).replaceAll('_',' ').toLowerCase()}` : 'Status unknown')} · ${esc(formatTimestamp(s.updated_at))}</strong><span class="sd-meta">${esc(s.host_id || 'Host not reported')} · ${esc(s.model || 'Model not reported')}</span></div>
      <p class="sd-meta">${terminal ? `${outcome === 'failed' ? 'Failed' : 'Finished'} session · recorded inspection remains available.` : 'Session status reflects the last report; it is not a live process check.'}</p>
      ${s.summary ? `<p style="overflow-wrap:anywhere">${esc(clip(s.summary))}</p>` : ''}
      ${s.mission_id || s.mission_key ? `<p class="sd-meta" id="session-mission">Mission association not resolved · ${esc(s.mission_key || s.mission_id)}</p>` : ''}
      ${relatedIntegrationLinks(s,'session',s.session_id)}
      <details class="hd-card sd-details" id="session-details"><summary>Session details and attachment</summary><dl>${fields.map(([label,value])=>`<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`).join('')}</dl>${s.summary ? `<p>${esc(s.summary)}</p>` : ''}${attach ? `<div class="hd-inline"><code style="flex:1;overflow-wrap:anywhere">${esc(attach)}</code><button class="hd-btn" id="copy-attach">Copy attach command</button></div>` : ''}</details>
      <h2>Inspection</h2><div id="inspector-host"></div>
      ${!terminal ? `<details class="hd-card sd-details"><summary>Send a message</summary><form id="msg-form" class="hd-inline"><input id="msg-input" aria-label="Message to session" placeholder="Steer the session…" style="flex:1;min-width:0;padding:8px"><button type="submit" class="hd-btn">Send</button></form></details>` : ''}
      <details class="hd-card sd-details" id="session-events"><summary>Recorded event stream</summary><div id="session-stream"><span class="muted">Loading recorded events…</span></div></details>`;
    disposeInspector = mountInspector(document.getElementById('inspector-host'), client, s.session_id);
    const missionElement = document.getElementById('session-mission');
    if (missionElement) {
      const contextController = new AbortController();
      sessionRequest = contextController;
      const contextDeadline = setTimeout(() => contextController.abort(), 15000);
      client.listMissions(undefined, {signal: contextController.signal}).then(response => {
        if (revision !== renderRevision || !missionElement.isConnected || contextController.signal.aborted) return;
        const match = sessionEntries([s], response.missions || [])[0]?.mission;
        if (match) missionElement.innerHTML = `Mission: <a href="#/missions/${esc(encodeURIComponent(match.key))}">${esc(clip(match.title))}</a>`;
      }).catch(() => { /* The unconfirmed association stays explicit. */ }).finally(() => {
        clearTimeout(contextDeadline);
        if (sessionRequest === contextController) sessionRequest = null;
      });
    }
    const stream = document.getElementById('session-stream');
    streamList('session',s.session_id).then(html=>{if(stream.isConnected && revision===renderRevision)stream.innerHTML=html;}).catch(()=>{if(stream.isConnected)stream.innerHTML='<span class="muted">Recorded events could not load.</span>';});
    const copyBtn = document.getElementById('copy-attach');
    if (copyBtn) copyBtn.addEventListener('click',()=>copyText(attach,copyBtn));
    document.getElementById('msg-form')?.addEventListener('submit',async event=>{
      event.preventDefault();const input=document.getElementById('msg-input');if(!input.value.trim())return;
      try {await client.sendMessage(s.session_id,input.value.trim());input.value='';const html=await streamList('session',s.session_id);if(stream.isConnected)stream.innerHTML=html;}
      catch(error){if(stream.isConnected)alert(error.message);}
    });
  } catch (error) {
    if (revision !== renderRevision) return;
    throw new Error(controller.signal.aborted ? 'Session details could not load. Try again.' : error.message);
  } finally {
    clearTimeout(deadline);
    if (sessionRequest === controller) sessionRequest = null;
  }
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
  servicesView = mountServices(root, client, services, { scope: _parts[0] === 'system-services' ? 'system' : 'herdr', showAll: route().query.get('all') === '1', status: route().query.get('status') || undefined });
}

async function viewService(parts, revision) {
  const svc = await client.request(`/api/v1/services/${encodeURIComponent(decodeURIComponent(parts[1]))}`);
  if (revision !== renderRevision) return;
  if (!svc || svc.error) { root.innerHTML = '<herdr-error message="Service not found"></herdr-error>'; return; }
  dashboardView = mountServiceDetails(root, client, svc);
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
    `/api/v1/events?entity_type=${encodeURIComponent(entityType)}&entity_id=${encodeURIComponent(entityId)}`);
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
    // Overview and Sessions own polling; SSE must preserve filters and focus.
    if (dashboardView) return;
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
        const sid = decodeURIComponent(route().parts.at(-1) || '');
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
