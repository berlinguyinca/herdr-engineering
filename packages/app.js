// HerdR Dev Fabric control-plane web app (spec 0190 §5, §20-34, §42-47).
// Hash-routed SPA. The UI is a *client* of the structured /api/v1 JSON + SSE
// stream. Views render loading / empty / error / data states (Spec §91).
import { HerdrClient } from './herdr-web-client/client.js';
import { connectEvents } from './herdr-web-client/events.js';
import { createRefreshBus } from './refresh-bus.js';
import { formatTimestamp } from './herdr-web-components/format.js';
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { defineComponents } from './herdr-web-components/index.js';
import { describeEvent, missionSubtitle, sessionSummary } from './herdr-web-components/humanize.js';
import { mountInspector } from './inspector-view.js';
import { mountSessionWorkspace } from './session-workspace.js';
import { mountTerminalAttach } from './terminal-attach.js';
import { mountMissionActivity } from './mission-live-view.js';
import { mountMissionCatalog } from './mission-catalog-view.js';
import { mountOverview } from './overview-view.js';
import { mountIntegrations } from './integrations-view.js';
import { mountGithub } from './github-view.js';
import { mountTopology } from './topology-view.js';
import { mountSessionsView, sessionEntries } from './sessions-view.js';
import { recordOutcome, statusOutcome } from './record-state.js';
import { RANGES, safeHostId } from './host-view.js';
import { mountHosts, loadHosts } from './hosts-view.js';
import { mountHostDetail, loadHostDetail } from './host-detail-view.js';
import { mountActivity, loadActivity } from './activity-view.js';
import { mountServices, mountServiceDetails } from './services-view.js';
import { mountCollab, completeSignInIfRedirected } from './collab-view.js';

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
  { path: '/herd', label: 'Agents' },
  { path: '/services', label: 'Services' },
  { path: '/local-services', label: 'Local services' },
  { path: '/system-services', label: 'System services' },
  { path: '/github', label: 'Projects' },
  { path: '/ci', label: 'CI' },
  { path: '/reviews', label: 'Reviews' },
  { path: '/hosts', label: 'Hosts' },
  { path: '/activity', label: 'Activity' },
  { path: '/analytics', label: 'Analytics' },
  { path: '/collab', label: 'Collaboration' },
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
  '/collab': viewCollab,
  '/ci': viewIntegrations,
  '/reviews': viewIntegrations,
  '/topology': viewTopology,
  '/missions': viewMissions,
  '/missions/:id': viewMission,
  '/herd': viewSessions,
  '/herd/session/:id': viewSession,
  '/services': viewServices,
  '/system-services': viewServices,
  '/local-services': viewServices,
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

// Router rule: render() runs only on a route change (any hash change, query
// included, since links like ?tab= / ?drift=1 still navigate). A same-route
// trigger (SSE event, tab becoming visible) goes through the refresh bus to
// currentView.refresh(hint) and never replaces the page; the spinner shows only
// on a route's first load.
//
// View contract: a view* function may return {refresh(hint), dispose()}. One
// that returns nothing gets NO_REFRESH, except the full-render routes below,
// which a compat view re-runs in place (no spinner) until Tasks 7-9 mount them.
const RERENDER_ROUTES = new Set(['/analytics', '/analytics/missions',
  '/analytics/models', '/analytics/hosts', '/analytics/tokens', '/missions/:id']);
const NO_REFRESH = { refresh() {}, dispose() {} };
// These dashboards own their polling; SSE must preserve their filters and focus.
const ownPolling = (view) => ({ refresh() {}, dispose: () => view.dispose() });

// Same throttle as before: SSE-driven refreshes re-fetch view data, so at most
// one per LIVE_REFRESH_MS per view keeps clear of the API's per-IP rate limit.
const LIVE_REFRESH_MS = 5000;
const bus = createRefreshBus({ throttleMs: LIVE_REFRESH_MS });
let currentView = null;
let unregisterView = null;
let sessionRequest = null;
let renderRevision = 0;

function matchRoute(parts) {
  return Object.keys(ROUTES).find((r) => {
    const rp = r.split('/').filter(Boolean);
    if (rp.length !== parts.length) return false;
    return rp.every((rpseg, i) => rpseg.startsWith(':') || rpseg === parts[i]);
  }) || '/overview';
}

function mountView(view) {
  currentView = view;
  // The view just loaded its data; the first event waits out one throttle window.
  unregisterView = bus.register(view, { fresh: true });
}

function unmountView() {
  if (unregisterView) { unregisterView(); unregisterView = null; }
  if (currentView) { currentView.dispose(); currentView = null; }
}

// Compat for routes not yet mounted: re-run the route's view function in place.
// A failed refresh keeps the last page; a refresh that yields a real view
// (e.g. retrying a page whose first load failed) adopts it.
function rerenderView(load) {
  let running = false;
  return {
    async refresh() {
      if (running) return;
      running = true;
      const revision = ++renderRevision;
      try {
        const view = await load(revision);
        if (view && revision === renderRevision) { unmountView(); mountView(view); }
        else if (view && revision !== renderRevision) view.dispose();
      } catch { /* keep the last page; the next event retries */ } finally { running = false; }
    },
    dispose() {},
  };
}

async function render() {
  const revision = ++renderRevision;
  unmountView();
  if (sessionRequest) { sessionRequest.abort(); sessionRequest = null; }
  const { parts } = route();
  const match = matchRoute(parts);
  const load = (rev) => ROUTES[match](parts, rev);
  root.innerHTML = '<herdr-spinner label="Loading"></herdr-spinner>';
  let view;
  try {
    view = await load(revision);
  } catch (err) {
    if (revision !== renderRevision) return;
    root.innerHTML = `<herdr-error message="${esc(err.message || 'request failed')}"></herdr-error>`;
    // As before, a live event retries a failed page, except pages that stream their own detail.
    const streamsOwnDetail = match === '/herd/session/:id' || (match === '/missions/:id' && decodeURIComponent(parts[1]).startsWith('pi:'));
    if (!streamsOwnDetail) mountView(rerenderView(load));
    return;
  }
  if (revision !== renderRevision) { view?.dispose(); return; }
  mountView(view || (RERENDER_ROUTES.has(match) ? rerenderView(load) : NO_REFRESH));
}

// ------------------------------------------------------------- views
// ------------------------------------------------------------- topology
async function viewTopology() {
  return ownPolling(mountTopology(root, client, {status: route().query.get('status') || 'current'}));
}

async function viewOverview() {
  root.innerHTML = '<h1>Overview</h1><div id="overview-dashboard"></div>';
  return ownPolling(mountOverview(document.getElementById('overview-dashboard'), client));
}

async function viewCollab() {
  return ownPolling(mountCollab(root, { fabricSessions: () => client.listSessions('active') }));
}

async function viewGithub() {
  return ownPolling(mountGithub(root, client));
}

async function viewIntegrations(parts) {
  const query = route().query;
  return ownPolling(mountIntegrations(root, client, {
    kind: parts[0], tab: query.get('tab') || 'current', search: query.get('search') || '',
    repository: query.get('repository') || '', provider: query.get('provider') || '', mission: query.get('mission') || '', session: query.get('session') || '',
  }));
}

async function viewMissions() {
  root.innerHTML = '<h1>Missions</h1><div id="live-missions"></div>';
  const catalog = mountMissionCatalog(document.getElementById('live-missions'), client, {tab: route().query.get('tab') || 'current', status: route().query.get('status') || 'all'});
  // Recorded lifecycle data refreshes separately so live telemetry and filters stay put.
  return { refresh: () => catalog.refreshRecorded(), dispose: () => catalog.dispose() };
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
    return { refresh() {}, dispose: mountMissionActivity(document.getElementById('live-mission-detail'), client, key) };
  }
  if (!m) { root.innerHTML = '<herdr-error message="Mission not found"></herdr-error>'; return; }
  root.innerHTML = `<a href="#/missions">← Missions</a><h1 style="overflow-wrap:anywhere">${key.startsWith('pi:') ? 'Pi mission' : esc((m.title || 'Mission').slice(0, 180))}</h1>${key.startsWith('pi:') ? '<div id="live-mission-detail"></div>' : ''}${relatedIntegrationLinks(m,'mission',key)}<div id="mission-history"></div>`;
  const disposeLive = key.startsWith('pi:') ? mountMissionActivity(document.getElementById('live-mission-detail'), client, key) : null;
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
      <span class="hd-card__sub"> · ${sessions} agents · ${esc(m.stage || 'Stage not recorded')} · created ${esc(formatTimestamp(m.created_at))}</span></div>
    <herdr-mission-activity updated-at="${esc(m.updated_at || '')}" source="${esc(m.source || '')}"></herdr-mission-activity>
    <details class="hd-card" data-history-panel="brief"><summary>Recorded mission brief</summary>
      <p style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(m.title || '')}</p>
      <div class="hd-card__sub">Scope</div><p style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(scope || 'No scope recorded for this mission.')}</p>
      ${m.cwd && m.cwd !== scope ? `<p style="overflow-wrap:anywhere"><code>${esc(m.cwd)}</code></p>` : ''}
      <herdr-ident kind="mission" id="${esc(m.mission_id)}"></herdr-ident>
    </details>
    <details class="hd-card" data-history-panel="sessions"><summary>Recorded agents · ${sessions}</summary>${sessionsHtml}</details>
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
  await refreshHistory(m);
  // Mission telemetry owns its polling; refreshing only the recorded history keeps disclosures open.
  // Non-Pi missions still re-render through the compat view until Task 9.
  if (disposeLive) return { refresh: () => refreshHistory(), dispose: disposeLive };
}

async function missionSessions(m) {
  const list = m.sessions || [];
  if (!list.length) return '<herdr-empty message="No agents on this mission yet"></herdr-empty>';
  const t = titles(await missionTitles());
  return list.map((s) => `
    <div class="hd-card">
      <div class="hd-card__title"><a href="#/herd/session/${esc(encodeURIComponent(s.session_id))}">${esc(s.display_name || s.agent_role || 'Agent')}</a></div>
      <div style="margin:6px 0"><herdr-status stage="${esc(s.status)}"></herdr-status></div>
      <div class="hd-card__sub">${esc(sessionSummary(s, t))}</div>
    </div>`).join('');
}

async function viewSessions() {
  return ownPolling(mountSessionsView(root, client, {tab: route().query.get('tab') || 'current'}));
}

async function viewSession(parts, revision) {
  const id = decodeURIComponent(parts[parts.length - 1]);
  const controller = new AbortController();
  sessionRequest = controller;
  const deadline = setTimeout(() => controller.abort(), 15000);
  try {
    const s = await client.getSession(id, {signal: controller.signal});
    if (revision !== renderRevision || controller.signal.aborted) return;
    if (!s) { root.innerHTML = '<herdr-error message="Agent not found"></herdr-error>'; return; }
    const outcome = recordOutcome(s);
    const terminal = outcome === 'failed' || outcome === 'finished';
    const reportedStatus = terminal && statusOutcome(s.status) !== outcome ? s.stage : s.status;
    const clip = value => typeof value === 'string' ? value.slice(0, 180) : '';
    const project = typeof s.cwd === 'string' ? s.cwd.replace(/\\/g, '/').split('/').filter(Boolean).pop() : '';
    const fields = [['Agent ID',s.session_id],['Last reported status',s.status],['Stage',s.stage],['Directory',s.cwd],['Git branch',s.branch],['PID',s.pid],['Command line',s.cmdline],['Model',s.model],['Last reported',formatTimestamp(s.updated_at)]].filter(([,v])=>v != null && v !== '');
    root.innerHTML = `<style>.sd-head{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:14px 0}.sd-meta{color:var(--text-secondary);line-height:1.6;overflow-wrap:anywhere}.sd-details{margin:14px 0;overflow-wrap:anywhere}.sd-details summary{cursor:pointer;font-weight:600}.sd-details dl{display:grid;grid-template-columns:minmax(100px,auto) minmax(0,1fr);gap:8px 16px}.sd-details dt{color:var(--text-secondary)}.sd-details dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.sd-title{overflow-wrap:anywhere}@media(max-width:600px){.sd-details dl{display:block}.sd-details dd{margin:4px 0 12px}}</style>
      <a href="#/herd?tab=${terminal ? outcome : 'current'}">← Agents</a>
      <h1 class="sd-title">${esc(clip(s.display_name || s.agent_role) || 'Agent')}${project ? ` · ${esc(clip(project))}` : ''}</h1>
      <div class="sd-head"><strong>${esc(reportedStatus ? `Last reported ${String(reportedStatus).replaceAll('_',' ').toLowerCase()}` : 'Status unknown')} · ${esc(formatTimestamp(s.updated_at))}</strong><span class="sd-meta">${esc(s.host_id || 'Host not reported')} · ${esc(s.model || 'Model not reported')}</span></div>
      <p class="sd-meta">${terminal ? `${outcome === 'failed' ? 'Failed' : 'Finished'} agent · recorded inspection remains available.` : 'Agent status reflects the last report; it is not a live process check.'}</p>
      ${s.summary ? `<p style="overflow-wrap:anywhere">${esc(clip(s.summary))}</p>` : ''}
      ${s.mission_id || s.mission_key ? `<p class="sd-meta" id="session-mission">Mission association not resolved · ${esc(s.mission_key || s.mission_id)}</p>` : ''}
      ${relatedIntegrationLinks(s,'session',s.session_id)}
      <div id="session-terminal" class="sd-details"></div>
      <details class="hd-card sd-details" id="session-details"><summary>Agent details</summary><dl>${fields.map(([label,value])=>`<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`).join('')}</dl>${s.summary ? `<p>${esc(s.summary)}</p>` : ''}</details>
      <div id="session-workspace-host"></div>
      <details class="hd-card sd-details" id="advanced-inspector"><summary>Advanced inspection</summary><div id="inspector-host"></div></details>
      <details class="hd-card sd-details" id="session-events"><summary>Recorded event stream</summary><div id="session-stream"><span class="muted">Loading recorded events…</span></div></details>`;
    const disposeWorkspace = mountSessionWorkspace(document.getElementById('session-workspace-host'), client, s, {onRename: profile => { s.display_name = profile.display_name; const title = root.querySelector('.sd-title'); if (title) title.textContent = `${profile.display_name}${project ? ` · ${clip(project)}` : ''}`; }});
    let disposeAdvanced = null;
    const advanced = document.getElementById('advanced-inspector');
    advanced.addEventListener('toggle', () => {
      if (advanced.open && !disposeAdvanced) disposeAdvanced = mountInspector(document.getElementById('inspector-host'), client, s.session_id);
      else if (!advanced.open && disposeAdvanced) { disposeAdvanced(); disposeAdvanced = null; document.getElementById('inspector-host').replaceChildren(); }
    });
    const disposeTerminal = mountTerminalAttach(document.getElementById('session-terminal'), client, s, {copyText});
    const disposeSession = () => { disposeWorkspace(); disposeTerminal(); if (disposeAdvanced) disposeAdvanced(); };
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
    // The page streams its own detail; a live event refreshes only the (cheap) recorded event list.
    return {
      async refresh() {
        if (!stream.isConnected) return;
        try { const html = await streamList('session', s.session_id); if (stream.isConnected) stream.innerHTML = html; } catch { /* keep last */ }
      },
      dispose: disposeSession,
    };
  } catch (error) {
    if (revision !== renderRevision) return;
    throw new Error(controller.signal.aborted ? 'Agent details could not load. Try again.' : error.message);
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
  return mountServices(root, client, services, { scope: ({'system-services':'system','local-services':'local'})[_parts[0]] || 'herdr', showAll: route().query.get('all') === '1', status: route().query.get('status') || undefined });
}

async function viewService(parts, revision) {
  const svc = await client.request(`/api/v1/services/${encodeURIComponent(decodeURIComponent(parts[1]))}`);
  if (revision !== renderRevision) return;
  if (!svc || svc.error) { root.innerHTML = '<herdr-error message="Service not found"></herdr-error>'; return; }
  return ownPolling(mountServiceDetails(root, client, svc));
}

async function viewHosts(_parts, revision) {
  const data = await loadHosts(client);
  if (revision !== renderRevision) return;
  return mountHosts(root, client, { data, driftedOnly: route().query.get('drift') === '1' });
}

async function viewHost(parts, revision) {
  const id = safeHostId(parts[1]);
  if (!id) { root.innerHTML = '<herdr-error message="Host not found"></herdr-error>'; return; }
  const requested = route().query.get('range');
  const range = RANGES.includes(requested) ? requested : '1h';
  const data = await loadHostDetail(client, id, range);
  if (revision !== renderRevision) return;
  return mountHostDetail(root, client, id, range, { data });
}

async function viewActivity(_parts, revision) {
  const data = await loadActivity(client, missionTitles);
  if (revision !== renderRevision) return;
  return mountActivity(root, client, { data, missionTitles });
}

async function viewAnalytics(_parts, revision) {
  const [missions, models, hosts] = await Promise.all([
    client.analytics('missions'), client.analytics('models'), client.analytics('hosts'),
  ]);
  if (revision !== renderRevision) return;
  root.innerHTML = `
    <style>
      .an-dashboard{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr));gap:var(--density-gap,16px);align-items:start;width:100%}.an-panel{min-width:0;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:var(--density-panel-padding,16px)}.an-panel h2{font-size:16px;margin:0 0 12px}.an-panel p{font-size:12px;color:var(--text-secondary)}
      .an-value{font-size:28px;font-weight:700;line-height:1.1}.an-value span{font-size:12px;font-weight:400;color:var(--text-secondary)}.an-pairs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.an-pairs div{min-width:0}.an-pairs strong{display:block;font-size:20px}.an-pairs span{font-size:12px;color:var(--text-secondary)}
      .an-links{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(160px,100%),1fr));gap:8px}.an-links a{display:flex;flex-direction:column;gap:4px;min-width:0;padding:12px;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm);text-decoration:none}.an-links a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.an-links strong{color:var(--text)}.an-links span{font-size:12px;color:var(--text-secondary)}
      .an-stage{grid-column:span 2}.an-model-list{margin:0;padding:0;list-style:none}.an-model-list li{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-top:1px solid var(--border-subtle);font-size:12px}.an-model-list li:first-child{border-top:0}.an-model-list span{overflow-wrap:anywhere}.an-stage .hd-card{padding:10px;margin-top:8px}
      @media(max-width:700px){.an-stage{grid-column:auto}.an-pairs{grid-template-columns:minmax(0,1fr)}}
    </style>
    <h1>Analytics</h1>
    <div class="an-dashboard">
      <section class="an-panel" data-analytics-panel="missions"><h2>Missions</h2><div class="an-value">${missions.total} <span>observed</span></div><p>${missions.completed} completed</p></section>
      <section class="an-panel" data-analytics-panel="completion"><h2>Completion</h2><div class="an-value">${missions.completion_rate}%</div><p>Across recorded missions</p></section>
      <section class="an-panel" data-analytics-panel="fleet"><h2>Fleet health</h2><div class="an-pairs"><div><strong>${hosts.healthy}</strong><span>healthy</span></div><div><strong>${hosts.unavailable}</strong><span>unavailable</span></div></div><p>${hosts.total} hosts reporting</p></section>
      <section class="an-panel" data-analytics-panel="models"><h2>Model coverage</h2><div class="an-value">${Object.keys(models.models).length} <span>models</span></div><p>${models.total_sessions} agent sessions</p><ul class="an-model-list">${Object.entries(models.models).slice(0, 4).map(([model, usage]) => `<li><span>${esc(model)}</span><strong>${usage.sessions}</strong></li>`).join('') || '<li><span>No model usage recorded</span></li>'}</ul></section>
      <section class="an-panel" data-analytics-panel="views"><h2>Explore analytics</h2><nav class="an-links" data-analytics-links aria-label="Analytics views"><a href="#/analytics/models"><strong>Models</strong><span>Agent distribution by model</span></a><a href="#/analytics/hosts"><strong>Hosts</strong><span>Reported fleet health</span></a><a href="#/analytics/tokens"><strong>Token usage</strong><span>Requests and tokens by project</span></a></nav></section>
      <section class="an-panel an-stage" data-analytics-panel="stages"><h2>Missions by stage</h2>${stageBars(missions.by_stage)}</section>
    </div>`;
}

async function viewAnalyticsMissions(parts, revision) { await viewAnalytics(parts, revision); }
async function viewAnalyticsModels(_parts, revision) {
  const { models, total_sessions } = await client.analytics('models');
  if (revision !== renderRevision) return;
  root.innerHTML = `<h1>Model analytics</h1><p>${total_sessions} total agents</p><div class="hd-grid">` +
    Object.entries(models).map(([model, u]) => `
      <div class="hd-card">
        <div class="hd-card__title">${esc(model)}</div>
        <div class="hd-card__sub">${u.sessions} agent(s) · ${u.messages} messages</div>
      </div>`).join('') + '</div>';
}
async function viewAnalyticsTokens(_parts, revision) {
  const t = await client.analytics('tokens');
  if (revision !== renderRevision) return;
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
async function viewAnalyticsHosts(_parts, revision) {
  const h = await client.analytics('hosts');
  if (revision !== renderRevision) return;
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

// Live updates (Spec §52, §85): SSE events feed the refresh bus, which calls
// the current view's refresh() at most once per LIVE_REFRESH_MS (first event
// after idle at once, the rest coalesced to the window's end). A hidden tab
// pauses the bus; becoming visible refreshes the view once.
connectEvents({
  url: '/api/v1/events/stream',
  onEvent: (event) => bus.dispatch(event),
  onError: () => { /* keep last view; the client auto-reconnects */ },
});

document.addEventListener('visibilitychange', () => { if (document.hidden) bus.pause(); else bus.resume(); });
if (document.hidden) bus.pause();

window.addEventListener('hashchange', () => render());
completeSignInIfRedirected().finally(() => render());
