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
import { mountReviewGate } from './review-gate-view.js';
import { mountTopology } from './topology-view.js';
import { mountSessionsView, sessionEntries } from './sessions-view.js';
import { recordOutcome, statusOutcome } from './record-state.js';
import { RANGES, safeHostId } from './host-view.js';
import { mountHosts, loadHosts } from './hosts-view.js';
import { mountHostDetail, loadHostDetail } from './host-detail-view.js';
import { mountActivity, loadActivity } from './activity-view.js';
import { mountAnalytics, loadAnalytics } from './analytics-view.js';
import { morph, patchList, patchText } from './patch.js';
import { flash, skeleton } from './motion.js';
import { mountServices, mountServiceDetails } from './services-view.js';
import { mountCollab, completeSignInIfRedirected } from './collab-view.js';
import { freshnessSources } from './freshness.js';
import { renderLlm, renderLlmSession, llmQuery, STEP_FOR, WINDOWS } from './llm-view.js';

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
  { path: '/review-gate', label: 'Review gate' },
  { path: '/hosts', label: 'Hosts' },
  { path: '/activity', label: 'Activity' },
  { path: '/analytics', label: 'Analytics' },
  { path: '/llm', label: 'LLM' },
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
  '/review-gate': viewReviewGate,
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
  '/analytics/missions': viewAnalytics,
  '/analytics/models': viewAnalytics,
  '/analytics/hosts': viewAnalytics,
  '/analytics/tokens': viewAnalytics,
  '/llm': viewLlm,
  '/llm/session/:id': viewLlmSession,
};

// Router rule: render() runs only on a route change (any hash change, query
// included, since links like ?tab= / ?drift=1 still navigate). A same-route
// trigger (SSE event, tab becoming visible) goes through the refresh bus to
// currentView.refresh(hint) and never replaces the page; the spinner shows only
// on a route's first load.
//
// View contract: a view* function may return {refresh(hint), dispose()}; one that
// returns nothing (an error or not-found page) is retried in place by rerenderView.
const NO_REFRESH = { refresh() {}, dispose() {} };

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

// Retry for a page whose first load failed or found nothing (e.g. "Mission not
// found"): re-run the route's view function in place on the next event. A failed
// retry keeps the page; a retry that yields a real view adopts it. A retry that
// never settles stops blocking new ones after RETRY_TIMEOUT_MS (its late result is
// discarded, since the next retry bumps the revision).
const RETRY_TIMEOUT_MS = 15000;
function rerenderView(load) {
  let running = false;
  return {
    async refresh() {
      if (running) return;
      running = true;
      const revision = ++renderRevision;
      const unwedge = setTimeout(() => { running = false; }, RETRY_TIMEOUT_MS);
      try {
        const view = await load(revision);
        if (view && revision === renderRevision) { unmountView(); mountView(view); }
        else if (view && revision !== renderRevision) view.dispose();
      } catch (err) {
        // Keep the last page; the next event retries. The page's chip says the retry failed, unless
        // the user has moved on: a stale retry must not mark the new page's chip.
        if (revision === renderRevision) root.querySelector('herdr-freshness')?.touch?.(false, err?.message || 'request failed');
      } finally {
        clearTimeout(unwedge);
        if (revision === renderRevision) running = false;
      }
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
  // As before, live events retry a failed or not-found page, except pages that stream their own detail.
  const streamsOwnDetail = match === '/herd/session/:id' || (match === '/missions/:id' && decodeURIComponent(parts[1]).startsWith('pi:'));
  root.innerHTML = routeSkeleton(match, parts);
  let view;
  try {
    view = await load(revision);
  } catch (err) {
    if (revision !== renderRevision) return;
    statusPage(`<herdr-error message="${esc(err.message || 'request failed')}"></herdr-error>`, false, err.message || 'request failed');
    if (!streamsOwnDetail) mountView(rerenderView(load));
    return;
  }
  if (revision !== renderRevision) { view?.dispose(); return; }
  mountView(view || (streamsOwnDetail ? NO_REFRESH : rerenderView(load)));
}

// Every routed page carries one freshness chip (<herdr-freshness>) in its head.
const CHIP = (interval = LIVE_REFRESH_MS) => `<herdr-freshness interval="${interval}"></herdr-freshness>`;
const liveHead = (title, interval) => `<div class="hd-live-head">${title}${CHIP(interval)}</div>`;
const chipIn = (el) => () => el.querySelector('herdr-freshness');
// Error and not-found pages have a chip too: red with the error, or fresh for a page that loaded
// and found nothing. A failed retry (rerenderView) marks it red again.
function statusPage(html, ok, message) {
  root.innerHTML = liveHead('') + html;
  root.querySelector('herdr-freshness').touch(ok, message);
}

// First-load placeholder for a route: its head (title bar, and the back link detail pages put
// above it) at the real page's position, then content rows sized roughly to the page that follows
// (a Pi mission without a record is short, as are model and host analytics; agent detail and the
// analytics dashboards fill a screen),
// so the page does not jump on arrival.
const BACK_LINK_SKELETON = '<div><span class="hd-skeleton hd-skeleton--line" style="display:inline-block;vertical-align:middle">&nbsp;</span></div>';
function routeSkeleton(match, parts) {
  const back = match === '/missions/:id' || match === '/herd/session/:id' ? BACK_LINK_SKELETON : '';
  const piMission = match === '/missions/:id' && decodeURIComponent(parts[1] || '').startsWith('pi:');
  const rows = piMission ? 1 : match === '/analytics/models' || match === '/analytics/hosts' ? 2 : match === '/herd/session/:id' || match === '/analytics' || match === '/analytics/missions' ? 8 : match.startsWith('/services') || match.endsWith('-services') ? 7 : 5;
  return `<div data-skeleton aria-busy="true" role="status" aria-label="Loading page">${back}<div class="hd-live-head"><div class="hd-skeleton hd-skeleton--title"></div></div>${skeleton('page content', { rows, height: 64, bare: true })}</div>`;
}

// ------------------------------------------------------------- views
// ------------------------------------------------------------- topology
async function viewTopology() {
  // Keeps its own 5 s poll per source; live events refresh it too.
  return mountTopology(root, client, {status: route().query.get('status') || 'current'});
}

async function viewOverview() {
  root.innerHTML = `${liveHead('<h1>Overview</h1>')}<div id="overview-dashboard"></div>`;
  // Polls each source every 5 s; live events refresh it too (a source already in flight is not re-read).
  return mountOverview(document.getElementById('overview-dashboard'), client, { freshness: chipIn(root) });
}

async function viewCollab() {
  // The chip sits outside the collab root: collab redraws its whole frame on every page change.
  root.innerHTML = `${liveHead('', 30000)}<div id="collab-root">${skeleton('collaboration', { rows: 8, height: 64 })}</div>`;
  const chip = chipIn(root);
  // The open room long-polls; live events make it re-ask at once.
  return mountCollab(document.getElementById('collab-root'), {
    fabricSessions: () => client.listSessions('active'),
    onFreshness: (ok, message) => chip()?.touch(ok, message),
  });
}

async function viewGithub() {
  // Keeps its own 5 s poll; live events refresh it too (requests in flight are not duplicated).
  return mountGithub(root, client);
}

async function viewReviewGate() {
  // Keeps its own poll; live events do not refresh it.
  const view = mountReviewGate(root, client);
  return { refresh() {}, dispose() { view.dispose(); } };
}

async function viewIntegrations(parts) {
  const query = route().query;
  // Keeps its own 5 s poll; live events refresh it (and the CI health map) too.
  return mountIntegrations(root, client, {
    kind: parts[0], tab: query.get('tab') || 'current', search: query.get('search') || '',
    repository: query.get('repository') || '', provider: query.get('provider') || '', mission: query.get('mission') || '', session: query.get('session') || '',
  });
}

async function viewMissions() {
  root.innerHTML = `${liveHead('<h1>Missions</h1>')}<div id="live-missions"></div>`;
  const catalog = mountMissionCatalog(document.getElementById('live-missions'), client, {tab: route().query.get('tab') || 'current', status: route().query.get('status') || 'all', freshness: chipIn(root)});
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
  // One chip for the page: the live telemetry and the recorded history are its two sources.
  const sources = freshnessSources(chipIn(root));
  const onStatus = (ok, message) => ok ? sources.ok('live') : sources.fail('live', `Live activity: ${message}`);
  if (key.startsWith('pi:') && !m) {
    root.innerHTML = `<a href="#/missions">← Missions</a>${liveHead('<h1>Pi mission</h1>')}${relatedIntegrationLinks({},'mission',key)}<div id="live-mission-detail"></div>`;
    return { refresh() {}, dispose: mountMissionActivity(document.getElementById('live-mission-detail'), client, key, { onStatus }) };
  }
  if (!m) { statusPage('<herdr-error message="Mission not found"></herdr-error>', true); return; }
  root.innerHTML = `<a href="#/missions">← Missions</a>${liveHead(`<h1 style="overflow-wrap:anywhere">${key.startsWith('pi:') ? 'Pi mission' : esc((m.title || 'Mission').slice(0, 180))}</h1>`)}${key.startsWith('pi:') ? '<div id="live-mission-detail"></div>' : ''}${relatedIntegrationLinks(m,'mission',key)}<div id="mission-history"></div>`;
  const disposeLive = key.startsWith('pi:') ? mountMissionActivity(document.getElementById('live-mission-detail'), client, key, { onStatus }) : null;
  const history = document.getElementById('mission-history');
  let pending = false;
  let shownTimeline = [null, ''];
  const refreshHistory = async (initial) => {
    if (pending || !history.isConnected || document.hidden) return;
    pending = true;
    try {
      const m = initial || await client.getMission(key);
      const sessions = (m.sessions || []).length;
      const scope = m.purpose || m.cwd || m.title;
      const [sessionsHtml, streamHtml] = await Promise.all([missionSessions(m), streamList('mission', missionKey(m))]);
      if (revision !== renderRevision || !history.isConnected) return;
      // Morphed in place: identical data writes nothing and open disclosures stay open.
      morph(history, `<h2>Recorded history</h2>
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
    <details class="hd-card" data-history-panel="events"><summary>Event stream</summary><div id="mission-stream">${streamHtml}</div></details>`);

      const timelineElement = history.querySelector('#mission-timeline');
      let items = [];
      try { items = (await client.missionTimeline(missionKey(m))).timeline || []; } catch { /* empty timeline */ }
      const itemsKey = JSON.stringify(items);
      if (timelineElement !== shownTimeline[0] || itemsKey !== shownTimeline[1]) { timelineElement.items = items; shownTimeline = [timelineElement, itemsKey]; }
      sources.ok('history');
    } catch (error) {
      if (history.isConnected) sources.fail('history', `Recorded history: ${error.message || 'request failed'}`);
      // A failed first load shows the error; a failed refresh keeps the last history.
      if (history.isConnected && !history.querySelector('details[data-history-panel]')) history.innerHTML = `<herdr-error message="${esc(error.message)}"></herdr-error>`;
    } finally { pending = false; }
  };
  await refreshHistory(m);
  // Mission telemetry owns its polling; a live event refreshes only the recorded history, in place.
  return { refresh: () => refreshHistory(), dispose: disposeLive || (() => {}) };
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
  // Keeps its own poll; live events refresh it too.
  return mountSessionsView(root, client, {tab: route().query.get('tab') || 'current'});
}

function sessionStatusText(s) {
  const outcome = recordOutcome(s);
  const terminal = outcome === 'failed' || outcome === 'finished';
  const reportedStatus = terminal && statusOutcome(s.status) !== outcome ? s.stage : s.status;
  return reportedStatus ? `Last reported ${String(reportedStatus).replaceAll('_',' ').toLowerCase()}` : 'Status unknown';
}

async function viewSession(parts, revision) {
  const id = decodeURIComponent(parts[parts.length - 1]);
  const controller = new AbortController();
  sessionRequest = controller;
  const deadline = setTimeout(() => controller.abort(), 15000);
  try {
    const s = await client.getSession(id, {signal: controller.signal});
    if (revision !== renderRevision || controller.signal.aborted) return;
    if (!s) { statusPage('<herdr-error message="Agent not found"></herdr-error>', true); return; }
    const outcome = recordOutcome(s);
    const terminal = outcome === 'failed' || outcome === 'finished';
    const clip = value => typeof value === 'string' ? value.slice(0, 180) : '';
    const project = typeof s.cwd === 'string' ? s.cwd.replace(/\\/g, '/').split('/').filter(Boolean).pop() : '';
    const fields = [['Agent ID',s.session_id],['Last reported status',s.status],['Stage',s.stage],['Directory',s.cwd],['Git branch',s.branch],['PID',s.pid],['Command line',s.cmdline],['Model',s.model],['Last reported',formatTimestamp(s.updated_at)]].filter(([,v])=>v != null && v !== '');
    root.innerHTML = `<style>.sd-head{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:14px 0}.sd-meta{color:var(--text-secondary);line-height:1.6;overflow-wrap:anywhere}.sd-details{margin:14px 0;overflow-wrap:anywhere}.sd-details summary{cursor:pointer;font-weight:600}.sd-details dl{display:grid;grid-template-columns:minmax(100px,auto) minmax(0,1fr);gap:8px 16px}.sd-details dt{color:var(--text-secondary)}.sd-details dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.sd-title{overflow-wrap:anywhere}@media(max-width:600px){.sd-details dl{display:block}.sd-details dd{margin:4px 0 12px}}</style>
      <a href="#/herd?tab=${terminal ? outcome : 'current'}">← Agents</a>
      ${liveHead(`<h1 class="sd-title">${esc(clip(s.display_name || s.agent_role) || 'Agent')}${project ? ` · ${esc(clip(project))}` : ''}</h1>`)}
      <div class="sd-head"><strong data-session-head><span data-session-status>${esc(sessionStatusText(s))}</span> · <span data-session-updated>${esc(formatTimestamp(s.updated_at))}</span></strong><span class="sd-meta">${esc(s.host_id || 'Host not reported')} · ${esc(s.model || 'Model not reported')}</span></div>
      <p class="sd-meta">${terminal ? `${outcome === 'failed' ? 'Failed' : 'Finished'} agent · recorded inspection remains available.` : 'Agent status reflects the last report; it is not a live process check.'}</p>
      ${s.summary ? `<p style="overflow-wrap:anywhere">${esc(clip(s.summary))}</p>` : ''}
      ${s.mission_id || s.mission_key ? `<p class="sd-meta" id="session-mission">Mission association not resolved · ${esc(s.mission_key || s.mission_id)}</p>` : ''}
      ${relatedIntegrationLinks(s,'session',s.session_id)}
      <div id="session-terminal" class="sd-details"></div>
      <details class="hd-card sd-details" id="session-details"><summary>Agent details</summary><dl>${fields.map(([label,value])=>`<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`).join('')}</dl>${s.summary ? `<p>${esc(s.summary)}</p>` : ''}</details>
      <div id="session-workspace-host"></div>
      <details class="hd-card sd-details" id="advanced-inspector"><summary>Advanced inspection</summary><div id="inspector-host"></div></details>
      <details class="hd-card sd-details" id="session-events"><summary>Recorded event stream</summary><div id="session-stream">${skeleton('recorded events', { rows: 2 })}</div></details>`;
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
    const head = root.querySelector('[data-session-head]');
    let headRequest = null;
    // The chip follows the two reads this page refreshes: the agent record and its recorded events.
    const sources = freshnessSources(chipIn(root));
    sources.ok('agent');
    streamItems('session',s.session_id).then(items=>{if(stream.isConnected && revision===renderRevision){paintStream(stream,items,{initial:true});sources.ok('events');}}).catch((error)=>{if(stream.isConnected){stream.innerHTML='<span class="muted">Recorded events could not load.</span>';sources.fail('events',`Recorded events: ${error.message || 'request failed'}`);}});
    // The page streams its own detail; a live event refreshes the recorded event list (new events
    // are appended) and the head status line, in place.
    return {
      async refresh() {
        if (!stream.isConnected) return;
        headRequest?.abort();
        const request = new AbortController(); headRequest = request;
        const failed = (source, label) => (error) => { if (!request.signal.aborted) sources.fail(source, `${label}: ${error?.message || 'request failed'}`); return null; };
        const [items, latest] = await Promise.all([
          streamItems('session', s.session_id).catch(failed('events', 'Recorded events')),
          client.getSession(id, { signal: request.signal }).catch(failed('agent', 'Agent')),
        ]);
        if (headRequest === request) headRequest = null;
        if (!stream.isConnected || request.signal.aborted) return;
        if (items) { paintStream(stream, items); sources.ok('events'); }
        if (latest) sources.ok('agent');
        if (!latest || !head.isConnected) return;
        // A heartbeat moves the report time only; the head flashes when the status itself changes.
        patchText(head.querySelector('[data-session-updated]'), formatTimestamp(latest.updated_at));
        if (patchText(head.querySelector('[data-session-status]'), sessionStatusText(latest))) flash(head);
      },
      dispose() { headRequest?.abort(); disposeSession(); },
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
  if (!svc || svc.error) { statusPage('<herdr-error message="Service not found"></herdr-error>', true); return; }
  // Keeps its own 30 s poll; live events refresh it too.
  return mountServiceDetails(root, client, svc);
}

async function viewHosts(_parts, revision) {
  const data = await loadHosts(client);
  if (revision !== renderRevision) return;
  return mountHosts(root, client, { data, driftedOnly: route().query.get('drift') === '1' });
}

async function viewHost(parts, revision) {
  const id = safeHostId(parts[1]);
  if (!id) { statusPage('<herdr-error message="Host not found"></herdr-error>', true); return; }
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

// /analytics and /analytics/{missions,models,tokens,hosts}: one mounted view per sub-page.
async function viewAnalytics(parts, revision) {
  const sub = parts[1] || '';
  const data = await loadAnalytics(client, sub);
  if (revision !== renderRevision) return;
  return mountAnalytics(root, client, sub, { data });
}

const NO_STREAM_EVENTS = '<herdr-empty message="No events for this entity yet"></herdr-empty>';

// The LLM pages load once (a filter change navigates). Their chip records that load and turns
// amber as it ages, which is the truth: the page does not refresh itself.
function liveLlmHead() {
  const h1 = root.querySelector('h1');
  if (!h1) return;
  h1.outerHTML = liveHead(h1.outerHTML);
  root.querySelector('herdr-freshness')?.touch(true);
}
async function viewLlm(_parts, revision) {
  const q = route().query;
  const window_ = WINDOWS.includes(q.get('window')) ? q.get('window') : '24h';
  const filters = { project: q.get('project') || '', model: q.get('model') || '' };
  const f = { window: window_, ...filters };
  const step = STEP_FOR[window_];
  const series = (metric) => client.llm('timeseries', { ...f, step, metric });
  const [total, models, agents, problems, projects, requests, errors, ttft, speed, sessions] = await Promise.all([
    client.llm('stats', { ...f, group: 'all' }),
    client.llm('stats', { window: window_, project: filters.project, group: 'model' }),
    client.llm('stats', { ...f, group: 'session' }),
    client.llm('errors', f),
    client.llm('stats', { window: window_, group: 'project' }),
    series('requests'), series('errors'), series('ttft_p95'), series('speed_p50'),
    client.listSessions().catch(() => ({ sessions: [] })),
  ]);
  if (revision !== renderRevision) return NO_REFRESH;
  const names = {};
  for (const s of sessions.sessions || []) if (s.display_name) names[s.session_id] = s.display_name;
  root.innerHTML = renderLlm({
    window: window_, filters, names,
    projects: projects.groups.map((g) => g.key),
    modelNames: [...new Set(models.groups.map((g) => g.key))],
  }, { total, models, agents, problems, series: { requests, errors, ttft, speed } });
  liveLlmHead();
  root.querySelectorAll('[data-llm-filter]').forEach((el) => el.addEventListener('change', () => {
    window.location.hash = `#/llm?${llmQuery({ window: window_, ...filters, [el.dataset.llmFilter]: el.value })}`;
  }));
  // A static page, as before the router change: a filter change navigates; live events do not redraw it.
  return NO_REFRESH;
}
async function viewLlmSession(parts, revision) {
  const id = decodeURIComponent(parts[2]);
  const q = route().query;
  const window_ = WINDOWS.includes(q.get('window')) ? q.get('window') : '24h';
  const f = { window: window_, session: id };
  const step = STEP_FOR[window_];
  const series = (metric) => client.llm('timeseries', { ...f, step, metric });
  const [total, models, problems, requests, errors, ttft, speed, session] = await Promise.all([
    client.llm('stats', { ...f, group: 'all' }), client.llm('stats', { ...f, group: 'model' }),
    client.llm('errors', f), series('requests'), series('errors'), series('ttft_p95'), series('speed_p50'),
    client.getSession(id).catch(() => null),
  ]);
  if (revision !== renderRevision) return NO_REFRESH;
  root.innerHTML = renderLlmSession({
    window: window_, sessionId: id, known: Boolean(session), name: session?.display_name || session?.agent_role || '',
  }, { total, models, problems, series: { requests, errors, ttft, speed } });
  liveLlmHead();
  return NO_REFRESH;
}

// Recorded events for one entity as keyed cards ({key, html}); keyed by sequence so a refresh
// keeps the cards already shown (morph pairs data-key, patchList appends only the new ones).
async function streamItems(entityType, entityId) {
  const { events } = await client.request(
    `/api/v1/events?entity_type=${encodeURIComponent(entityType)}&entity_id=${encodeURIComponent(entityId)}`);
  if (!events.length) return [];
  const t = titles(await missionTitles());
  return events.map((e) => {
    const d = describeEvent(e, { missionTitles: t });
    const key = String(e.sequence ?? e.event_id ?? '');
    return { key, html: `<div class="hd-card" data-key="${esc(key)}">
      <div class="hd-card__title">${esc(d.title)}</div>
      <div class="hd-card__sub">${esc(d.detail)}</div>
      <div class="hd-card__sub">seq ${e.sequence} · ${formatTimestamp(e.source_timestamp)}</div>
    </div>` };
  });
}

async function streamList(entityType, entityId) {
  const items = await streamItems(entityType, entityId);
  return items.length ? items.map((item) => item.html).join('') : NO_STREAM_EVENTS;
}

// Paints recorded events into a container in place: new events are appended (and enter, unless
// calm), cards already shown keep their nodes, and the container itself is never replaced.
function paintStream(container, items, { initial = false } = {}) {
  if (!items.length) { if (container.innerHTML !== NO_STREAM_EVENTS) container.innerHTML = NO_STREAM_EVENTS; return; }
  for (const child of [...container.children]) if (!child.hasAttribute('data-key')) child.remove();
  patchList(container, items, { key: (item) => item.key, render: (item) => item.html, animate: !initial });
  if (initial) for (const el of container.querySelectorAll('[data-entering]')) el.removeAttribute('data-entering');
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
