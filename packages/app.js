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
import { renderFleetMatrix } from './fleet-view.js';
import { renderHostDetail, renderHostCard, HOST_CARD_STYLE, RANGES, safeHostId } from './host-view.js';
import { configBadge, configNotice, indexConfigStatus } from './fleet-config-badge.js';
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

async function viewCollab() {
  dashboardView = mountCollab(root, { fabricSessions: () => client.listSessions('active') });
}

async function viewGithub() {
  dashboardView = mountGithub(root, client);
}

async function viewIntegrations(parts) {
  const query = route().query;
  dashboardView = mountIntegrations(root, client, {
    kind: parts[0], tab: query.get('tab') || 'current', search: query.get('search') || '',
    repository: query.get('repository') || '', provider: query.get('provider') || '', mission: query.get('mission') || '', session: query.get('session') || '',
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
  if (key.startsWith('pi:')) refreshMissionHistory = refreshHistory;
  await refreshHistory(m);
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
    disposeInspector = () => { disposeWorkspace(); disposeTerminal(); if (disposeAdvanced) disposeAdvanced(); };
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
  servicesView = mountServices(root, client, services, { scope: ({'system-services':'system','local-services':'local'})[_parts[0]] || 'herdr', showAll: route().query.get('all') === '1', status: route().query.get('status') || undefined });
}

async function viewService(parts, revision) {
  const svc = await client.request(`/api/v1/services/${encodeURIComponent(decodeURIComponent(parts[1]))}`);
  if (revision !== renderRevision) return;
  if (!svc || svc.error) { root.innerHTML = '<herdr-error message="Service not found"></herdr-error>'; return; }
  dashboardView = mountServiceDetails(root, client, svc);
}

async function viewHosts() {
  const [{ hosts }, matrix, configStatus] = await Promise.all([
    client.listHosts(),
    client.request('/api/v1/fleet/inventory').catch(() => ({ hosts: [], components: [] })),
    client.getFleetConfigStatus().catch(() => null),
  ]);
  const cfg = indexConfigStatus(configStatus);
  if (!hosts.length) { root.innerHTML = '<herdr-empty message="No hosts reporting"></herdr-empty>'; return; }
  const driftedOnly = route().query.get('drift') === '1';
  const now = Date.now();
  root.innerHTML = `<h1>Hosts</h1>
    ${HOST_CARD_STYLE}${configNotice(cfg)}
    <div class="hd-grid">` + hosts.map((h) => renderHostCard(h, { now, cfg })).join('') + `</div>
    <h2 style="margin-top:24px">Fleet versions</h2>
    <p class="hd-card__sub"><a href="#/hosts${driftedOnly ? '' : '?drift=1'}">${driftedOnly ? 'Show all components' : 'Show only components that differ'}</a></p>
    ${renderFleetMatrix(matrix, { driftedOnly })}`;
}

async function viewHost(parts, revision) {
  const id = safeHostId(parts[1]);
  if (!id) { root.innerHTML = '<herdr-error message="Host not found"></herdr-error>'; return; }
  const requested = route().query.get('range');
  const range = RANGES.includes(requested) ? requested : '1h';
  // Only the host fetch may fail the page; missing history degrades to empty sparklines.
  const [h, telemetry] = await Promise.all([
    client.request(`/api/v1/hosts/${id}`).catch((err) => { if (err?.status === 404) return null; throw err; }),
    client.request(`/api/v1/hosts/${id}/telemetry?range=${range}`).catch(() => ({ samples: [] })),
  ]);
  if (revision !== renderRevision) return;
  if (!h || h.error) { root.innerHTML = '<herdr-error message="Host not found"></herdr-error>'; return; }
  root.innerHTML = renderHostDetail(h, telemetry, { range, now: Date.now() });
}

async function viewActivity() {
  const { events } = await client.activity(50);
  if (!events.length) {
    root.innerHTML = '<h1>Activity</h1><herdr-empty message="No activity yet"></herdr-empty>';
    return;
  }
  const missionTitleLookup = titles(await missionTitles());
  const entries = events.map((event) => {
    const description = describeEvent(event, { missionTitles: missionTitleLookup });
    const rawPayload = JSON.stringify(event.payload || {}, null, 2);
    return {
      event,
      description,
      payload: rawPayload.length > 8000 ? `${rawPayload.slice(0, 7999)}…` : rawPayload,
    };
  });
  const counts = new Map();
  for (const { event } of entries) counts.set(event.event_type, (counts.get(event.event_type) || 0) + 1);
  const types = [...counts.keys()];
  const selectedDetail = ({ event, description, payload }) => `<div class="ac-selected-head"><div><span class="ac-eyebrow">Selected event</span><h2>${esc(description.title)}</h2></div><time>${formatTimestamp(event.source_timestamp)}</time></div>
    <dl class="ac-selected-meta"><div><dt>Type</dt><dd>${esc(event.event_type || 'Event')}</dd></div><div><dt>Event ID</dt><dd><code>${esc(event.event_id || 'unavailable')}</code></dd></div></dl>
    <p class="ac-selected-detail">${esc(description.detail)}</p><pre class="ac-payload" aria-label="Selected event payload">${esc(payload)}</pre>`;
  root.innerHTML = `<style>
    .ac-workspace{display:grid;grid-template-columns:minmax(620px,1600px) minmax(320px,1fr);gap:var(--density-gap,16px);align-items:start;width:100%}
    .ac-stream{min-width:0}.ac-stream-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:8px}.ac-stream-head h2{margin:0}.ac-stream-head span,.ac-eyebrow{font-size:12px;color:var(--text-secondary)}
    .ac-list{min-width:0;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden}
    .ac-row{border-bottom:1px solid var(--border-subtle)}.ac-row:last-child{border-bottom:0}.ac-row[open]{background:var(--bg)}
    .ac-row summary{display:grid;grid-template-columns:minmax(140px,220px) minmax(0,1fr) auto;gap:14px;align-items:center;padding:10px 12px;cursor:pointer;list-style-position:inside;font-size:13px}
    .ac-row summary strong{overflow-wrap:anywhere}.ac-kind,.ac-time{font-size:12px;color:var(--text-secondary)}.ac-time{white-space:nowrap}
    .ac-detail{margin:0;padding:0 32px 8px;font-size:12px;line-height:1.5;color:var(--text-secondary);overflow-wrap:anywhere}.ac-payload{margin:0 32px 12px;max-height:240px;overflow:auto;white-space:pre-wrap;font-size:11px;line-height:1.45;background:var(--surface);border:1px solid var(--border-subtle);border-radius:var(--radius-sm);padding:10px}
    .ac-row[data-muted=true]{opacity:.65}
    .ac-rail{position:sticky;top:16px;display:grid;gap:var(--density-gap,16px);min-width:0}.ac-panel{min-width:0;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:var(--density-panel-padding,16px)}.ac-panel h2{font-size:16px;margin:0 0 10px}.ac-panel p{color:var(--text-secondary);line-height:1.5}
    .ac-filter-list{display:flex;flex-wrap:wrap;gap:8px;min-width:0}.ac-filter{min-height:36px;max-width:100%;border:1px solid var(--border);border-radius:999px;background:var(--bg);color:var(--text);padding:6px 10px;cursor:pointer;overflow-wrap:anywhere;white-space:normal}.ac-filter[aria-pressed=true]{border-color:var(--accent);background:var(--accent);color:var(--accent-contrast,#fff)}
    .ac-repeat-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:14px 0 0;padding:0;list-style:none}.ac-repeat-list li{display:flex;justify-content:space-between;gap:8px;min-width:0;border-top:1px solid var(--border-subtle);padding-top:8px;font-size:12px}.ac-repeat-list li span{min-width:0;overflow-wrap:anywhere}.ac-repeat-list strong{flex:0 0 auto;font-size:14px}.ac-filter-status{margin:10px 0 0;font-size:12px;color:var(--text-secondary)}
    .ac-selected{min-width:0}.ac-selected>*{min-width:0;max-width:100%;box-sizing:border-box}.ac-selected-head{display:flex;justify-content:space-between;gap:12px;align-items:start}.ac-selected-head>div{min-width:0;max-width:100%}.ac-selected-head h2{min-width:0;margin:3px 0 0;overflow-wrap:anywhere;word-break:break-word}.ac-selected-head time{flex:0 0 auto;font-size:12px;color:var(--text-secondary);white-space:nowrap}.ac-selected-meta{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:14px 0}.ac-selected-meta div{min-width:0}.ac-selected-meta dt{font-size:11px;color:var(--text-secondary)}.ac-selected-meta dd{margin:3px 0 0;overflow-wrap:anywhere}.ac-selected-detail{overflow-wrap:anywhere;word-break:break-word}.ac-selected .ac-payload{margin:12px 0 0;max-width:100%;max-height:360px;box-sizing:border-box;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}
    .ac-selection-status{position:fixed;inset:0 auto auto 0;width:1px;max-width:1px;height:1px;padding:0;margin:0;overflow:hidden;overflow-wrap:anywhere;word-break:break-all;clip-path:inset(50%);white-space:normal;border:0;contain:strict}
    @media(min-width:2200px){.ac-workspace{grid-template-columns:minmax(900px,1600px) minmax(760px,1fr)}.ac-rail{grid-template-columns:minmax(260px,.7fr) minmax(360px,1.3fr)}}
    @media(max-width:1300px){.ac-workspace{grid-template-columns:minmax(0,1fr)}.ac-rail{position:static;grid-template-columns:repeat(2,minmax(0,1fr))}}
    @media(max-width:650px){.ac-rail{grid-template-columns:minmax(0,1fr)}.ac-row summary{grid-template-columns:minmax(0,1fr);gap:4px;padding:12px}.ac-row summary>*{min-width:0}.ac-kind,.ac-time{grid-column:1}.ac-time{white-space:normal;overflow-wrap:anywhere}.ac-detail{padding:0 12px 8px}.ac-payload{margin:0 12px 12px;overflow-wrap:anywhere}.ac-selected-meta{grid-template-columns:minmax(0,1fr)}.ac-selected-head{display:block}.ac-selected-head time{display:block;margin-top:6px}}
  </style><h1>Activity</h1><div class="ac-workspace">
    <section class="ac-stream" aria-labelledby="activity-stream-title"><div class="ac-stream-head"><h2 id="activity-stream-title">Chronological stream</h2><span>${entries.length} events</span></div>
      <div class="ac-list">${entries.map(({ event, description, payload }, index) => `<details class="ac-row" data-event-id="${esc(event.event_id || '')}" data-event-type="${esc(event.event_type || '')}" data-entry-index="${index}" data-muted="${description.muted}"><summary><strong>${esc(description.title)}</strong><span class="ac-kind">${esc(description.kind)}</span><time class="ac-time">${formatTimestamp(event.source_timestamp)}</time></summary><p class="ac-detail">${esc(description.detail)}</p><pre class="ac-payload" aria-label="Original event payload">${esc(payload)}</pre></details>`).join('')}</div>
    </section>
    <aside class="ac-rail" aria-label="Activity summary and selected event">
      <section class="ac-panel"><h2>Event types</h2><div class="ac-filter-list" role="group" aria-label="Filter activity by event type"><button class="ac-filter" type="button" data-event-filter="all" aria-pressed="true">All <strong>${entries.length}</strong></button>${types.map((type) => `<button class="ac-filter" type="button" data-event-filter="${esc(type)}" aria-pressed="false">${esc(type)} <strong>${counts.get(type)}</strong></button>`).join('')}</div>
        <p class="ac-filter-status" role="status">Showing all ${entries.length} events</p>
        <h2 style="margin-top:18px">Repeated types</h2><ul class="ac-repeat-list">${types.filter((type) => counts.get(type) > 1).map((type) => `<li data-repeat-type="${esc(type)}"><span>${esc(type)}</span><strong>${counts.get(type)}</strong></li>`).join('') || '<li><span>No repeated event types</span></li>'}</ul>
      </section>
      <p class="ac-selection-status" data-activity-selection-status role="status" aria-live="polite">Selected event: ${esc(entries[0].description.title)}</p>
      <section class="ac-panel ac-selected" data-activity-selected data-entry-index="0">${selectedDetail(entries[0])}</section>
    </aside>
  </div>`;

  const rows = [...root.querySelectorAll('.ac-row')];
  const selected = root.querySelector('[data-activity-selected]');
  const selectionStatus = root.querySelector('[data-activity-selection-status]');
  const showSelected = (row) => {
    const entry = entries[Number(row.dataset.entryIndex)];
    if (!entry) return;
    selected.dataset.entryIndex = row.dataset.entryIndex;
    selected.innerHTML = selectedDetail(entry);
    selectionStatus.textContent = `Selected event: ${entry.description.title}`;
  };
  for (const row of rows) {
    row.querySelector('summary').addEventListener('focus', () => showSelected(row));
    row.addEventListener('toggle', () => { if (row.open) showSelected(row); });
  }
  const filters = [...root.querySelectorAll('[data-event-filter]')];
  const filterStatus = root.querySelector('.ac-filter-status');
  for (const button of filters) button.addEventListener('click', () => {
    const type = button.dataset.eventFilter;
    let shown = 0;
    for (const row of rows) {
      row.hidden = type !== 'all' && row.dataset.eventType !== type;
      if (!row.hidden) shown += 1;
    }
    for (const candidate of filters) candidate.setAttribute('aria-pressed', String(candidate === button));
    filterStatus.textContent = type === 'all' ? `Showing all ${shown} events` : `Showing ${shown} ${type} events`;
    const selectedRow = rows[Number(selected.dataset.entryIndex)];
    if (!selectedRow || selectedRow.hidden) showSelected(rows.find((row) => !row.hidden));
  });
}

async function viewAnalytics() {
  const [missions, models, hosts] = await Promise.all([
    client.analytics('missions'), client.analytics('models'), client.analytics('hosts'),
  ]);
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

async function viewAnalyticsMissions() { await viewAnalytics(); }
async function viewAnalyticsModels() {
  const { models, total_sessions } = await client.analytics('models');
  root.innerHTML = `<h1>Model analytics</h1><p>${total_sessions} total agents</p><div class="hd-grid">` +
    Object.entries(models).map(([model, u]) => `
      <div class="hd-card">
        <div class="hd-card__title">${esc(model)}</div>
        <div class="hd-card__sub">${u.sessions} agent(s) · ${u.messages} messages</div>
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
      liveTimer = null;
      lastLiveRender = Date.now();
      // The event may have arrived while the view was still loading; a view that
      // mounted since then owns its refresh (a full re-render would reset filters).
      if (servicesView) { servicesView.refresh(); return; }
      if (dashboardView) return;
      render();
    }, wait);
  },
  onError: () => { /* keep last view; the client auto-reconnects */ },
});

window.addEventListener('hashchange', () => {
  if (liveTimer) { clearTimeout(liveTimer); liveTimer = null; }
  lastLiveRender = Date.now(); render();
});
completeSignInIfRedirected().finally(() => render());
