// HerdR Dev Fabric control-plane web app (spec 0190 §5, §20-34, §42-47).
// Hash-routed SPA. The UI is a *client* of the structured /api/v1 JSON + SSE
// stream. Views render loading / empty / error / data states (Spec §91).
import { HerdrClient } from './herdr-web-client/client.js';
import { connectEvents } from './herdr-web-client/events.js';
import { createRefreshBus } from './refresh-bus.js';
import { formatTimestamp, formatDuration } from './herdr-web-components/format.js';
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { defineComponents } from './herdr-web-components/index.js';
import { missionSubtitle, sessionSummary } from './herdr-web-components/humanize.js';
import { mountInspector } from './inspector-view.js';
import { mountSessionWorkspace } from './session-workspace.js';
import { mountTerminalAttach } from './terminal-attach.js';
import { mountMissionActivity } from './mission-live-view.js';
import { mountMissionCatalog } from './mission-catalog-view.js';
import { mountOverview } from './overview-view.js';
import { mountIntegrations } from './integrations-view.js';
import { mountGithub } from './github-view.js';
import { mountGithubProjects } from './github-projects-view.js';
import { mountReviewGate } from './review-gate-view.js';
import { mountTopology } from './topology-view.js';
import { mountArchitecture } from './architecture-view.js';
import { mountPersonalAssistant } from './personal-assistant-view.js';
import { mountProjectIntelligence } from './project-intelligence-view.js';
import { visibleNav } from './nav-capabilities.js';
import { mountSessionsView, sessionEntries } from './sessions-view.js';
import { agentStory, renderStory } from './agents-lineage.js';
import { recordOutcome, statusOutcome } from './record-state.js';
import { RANGES, safeHostId } from './host-view.js';
import { mountHosts, loadHosts } from './hosts-view.js';
import { mountHostDetail, loadHostDetail } from './host-detail-view.js';
import { mountActivity, loadActivity } from './activity-view.js';
import { mountAnalytics, loadAnalytics, usageStrip } from './analytics-view.js';
import { morph, patchText } from './patch.js';
import { flash, skeleton } from './motion.js';
import { noteStreamContact, freshnessSources } from './freshness.js';
import { mountServices, mountServiceDetails } from './services-view.js';
import { mountFabricServices } from './fabric-services-view.js';
import { mountCollab, completeSignInIfRedirected } from './collab-view.js';
import { mountAttention, attentionBadge, POLL_MS as ATTENTION_POLL_MS } from './attention-view.js';
import { createAttentionNotifier } from './attention-notify.js';
import { mountRelated } from './related-panel.js';
import { mountMissionStory } from './mission-story-view.js';
import { renderLlm, renderLlmSession, llmQuery, STEP_FOR, WINDOWS } from './llm-view.js';
import { mountArtifacts, renderArtifactDetail } from './artifacts-view.js';
import { mountPublications } from './publications-view.js';
import { mountHandoffs } from './handoffs-view.js';
import { mountScheduling } from './scheduler-view.js';
import { mountDeployments } from './deployments-view.js';
import { classifyDashboardError, dashboardErrorMessage } from './dashboard-load-state.js';
import { mountEventHistory } from './event-history.js';

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

// Sidebar sections. A `tabs` entry owns a section whose other pages show as a tab strip above the page (SECTION_TABS), so
// related pages (the four service views, plan reviews + review gate, ...) take one sidebar row instead of several.
const NAV = [
  { group: 'Monitor', path: '/overview', label: 'Overview' },
  { group: 'Monitor', path: '/attention', label: 'Attention', match: ['/activity'] },
  { group: 'Agents', path: '/herd', label: 'Agents', match: ['/missions', '/handoffs', '/topology'] },
  { group: 'Agents', path: '/collab', label: 'Collaboration' },
  { group: 'Agents', path: '/personal-assistant', label: 'Assistant', capability: 'assistant_v1' },
  { group: 'Fabric', path: '/hosts', label: 'Hosts', match: ['/scheduling'] },
  { group: 'Fabric', path: '/services', label: 'Services', match: ['/fabric-services', '/local-services', '/system-services'] },
  { group: 'Fabric', path: '/llm', label: 'Usage', match: ['/analytics'] },
  { group: 'Delivery', path: '/ci', label: 'CI', match: ['/deployments'] },
  { group: 'Delivery', path: '/reviews', label: 'Reviews', match: ['/review-gate'] },
  { group: 'Delivery', path: '/github', label: 'Projects', match: ['/github/projects'] },
  { group: 'Code', path: '/architecture', label: 'Architecture', capability: 'architecture_v1' },
  { group: 'Code', path: '/artifacts', label: 'Artifacts', match: ['/publications'] },
  { group: 'Code', path: '/intelligence', label: 'Project intelligence', capability: 'project_intelligence_v1' },
];
// Handoffs are the thread through the Agents section: who started whom, and why. Topology is no longer listed (the lineage
// graph replaces it); its route still answers for old links.
const SECTION_TABS = [
  [['/herd', 'Agents'], ['/missions', 'Missions'], ['/handoffs', 'Handoffs']],
  [['/attention', 'Attention'], ['/activity', 'Activity']],
  [['/hosts', 'Hosts'], ['/scheduling', 'Scheduling']],
  [['/services', 'Catalog'], ['/fabric-services', 'Fabric'], ['/local-services', 'Local'], ['/system-services', 'System']],
  [['/ci', 'CI'], ['/deployments', 'Deployments']],
  [['/reviews', 'Plan reviews'], ['/review-gate', 'Review gate']],
  [['/github', 'Issues'], ['/github/projects', 'Boards']],
  [['/artifacts', 'Artifacts'], ['/publications', 'Publications']],
];

const SHELL = document.getElementById('shell');
// A page is listed only while it works. `hd-freshness` reports each page's last load: a missing backend (404/501, "not enabled",
// "disabled") marks that exact page or tab, a later success clears it, and timeouts or other errors never mark anything (they are
// transient). Only a page's own route counts: a detail route (/missions/x, /hosts/y) failing says nothing about its section. A
// sidebar entry disappears only when its own page and every tab of its section are marked, and a marked tab drops out of the tab
// strip. "Show unavailable" in the display footer lists everything again. Last-known state lives in localStorage.
const UNAVAILABLE_KEY = 'herdr-unavailable-pages';
let capabilities = [];
let unavailable = new Set();
let showUnavailable = false;
try { unavailable = new Set(JSON.parse(window.localStorage.getItem(UNAVAILABLE_KEY) || '[]')); } catch { /* Nothing is hidden then. */ }
const MISSING = /not found|\b(404|501)\b|not implemented|not enabled|is disabled/i;
const routePath = () => (window.location.hash.replace(/^#/, '') || '/overview').split('?')[0];
const KNOWN_PAGES = new Set([...NAV.flatMap((e) => [e.path, ...(e.match || [])]), ...SECTION_TABS.flat().map(([p]) => p)]);
const entryPages = (entry) => [entry.path, ...(entry.match || [])].filter((p) => KNOWN_PAGES.has(p));
const SUBNAV = document.getElementById('subnav');
function renderSubnav() {
  const path = (window.location.hash.replace(/^#/, '') || '/overview').split('?')[0];
  const tabs = SECTION_TABS.find((t) => t.some(([p]) => path === p || path.startsWith(p + '/')));
  if (!tabs || !SUBNAV) { if (SUBNAV) { SUBNAV.hidden = true; SUBNAV.textContent = ''; } return; }
  // The longest matching path wins, so /github/projects is not also an active "Issues" tab.
  const current = tabs.map(([p]) => p).filter((p) => path === p || path.startsWith(p + '/')).sort((x, y) => y.length - x.length)[0];
  SUBNAV.hidden = false;
  const listed = tabs.filter(([p]) => showUnavailable || p === current || !unavailable.has(p));
  SUBNAV.hidden = listed.length < 2;
  SUBNAV.innerHTML = listed.map(([p, label]) => `<a href="#${p}"${p === current ? ' aria-current="page"' : ''}>${label}</a>`).join('');
}
function currentNavPath() {
  const path = routePath();
  return NAV.find((e) => [e.path, ...(e.match || [])].some((p) => path === p || path.startsWith(p + '/')))?.path || '';
}
function navRoutes() {
  const list = visibleNav(NAV, capabilities);
  if (showUnavailable) return list;
  const here = currentNavPath();
  return list.filter((entry) => entry.path === here || !entryPages(entry).every((p) => unavailable.has(p)));
}
function applyRoutes() {
  const routes = JSON.stringify(navRoutes());
  if (routes !== SHELL.getAttribute('routes')) SHELL.setAttribute('routes', routes);
  const toggle = document.getElementById('show-unavailable');
  if (toggle) { toggle.hidden = !unavailable.size; toggle.textContent = showUnavailable ? 'Hide unavailable' : `Show unavailable (${unavailable.size})`; }
  renderSubnav();
}
function markAvailability(ok, message) {
  const path = routePath();
  if (!KNOWN_PAGES.has(path) || path === '/overview' || path === '/attention') return;
  const missing = !ok && MISSING.test(message || '');
  if (!ok && !missing) return;
  if (missing === unavailable.has(path)) return;
  if (missing) unavailable.add(path); else unavailable.delete(path);
  try { window.localStorage.setItem(UNAVAILABLE_KEY, JSON.stringify([...unavailable])); } catch { /* Kept for this page only. */ }
  applyRoutes();
}
root.addEventListener('hd-freshness', (event) => markAvailability(event.detail.ok, event.detail.message));
{
  const toggle = document.createElement('button');
  toggle.type = 'button'; toggle.id = 'show-unavailable'; toggle.slot = 'controls'; toggle.hidden = true; toggle.className = 'hd-linkbtn';
  toggle.addEventListener('click', () => { showUnavailable = !showUnavailable; applyRoutes(); });
  SHELL.append(toggle);
}
window.addEventListener('hashchange', applyRoutes);   // the entry you are on stays listed; leaving it lets it go
applyRoutes();
// Optional tabs appear once /api/v1/health advertises their capability; a failed probe keeps them hidden.
client.health().then((h) => { capabilities = h?.capabilities || []; applyRoutes(); }).catch(() => { /* optional tabs stay hidden */ });
SHELL.setAttribute('brand', 'HerdR Dev Fabric');

// The nav badge follows the server inbox on every page, so a new critical item is visible from anywhere.
function setAttentionBadge(badge) { SHELL.setBadge?.('/attention', badge); }
let attentionStorage = null;
try { attentionStorage = window.localStorage; } catch { /* Notification choices then last for this page only. */ }
const attentionNotifier = createAttentionNotifier({
  storage: attentionStorage, NotificationImpl: window.Notification,
  onClick: (href) => { window.focus(); window.location.hash = href; },
});
function handleAttention(body) { setAttentionBadge(attentionBadge(body)); attentionNotifier.handle(body); }
let attentionBadgeTimer = null;
async function pollAttentionBadge() {
  clearTimeout(attentionBadgeTimer);
  // A hidden tab keeps polling only when browser notifications are on, so a new critical item still reaches the operator.
  if (!document.hidden || attentionNotifier.active()) {
    try { handleAttention(await client.attention({ state: 'open' })); }
    catch { /* The page itself explains an unreachable inbox; the badge just stays as it was. */ }
  }
  attentionBadgeTimer = setTimeout(pollAttentionBadge, ATTENTION_POLL_MS * 3);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) pollAttentionBadge(); });
pollAttentionBadge();

function route() {
  const hash = window.location.hash.replace(/^#/, '') || '/overview';
  const [path, query] = hash.split('?');
  const parts = path.split('/').filter(Boolean); // ['missions', id]
  return { path: path || '/overview', parts, query: new URLSearchParams(query) };
}

const ROUTES = {
  '/overview': viewOverview,
  '/attention': viewAttention,
  '/github/projects': viewGithubProjects,
  '/github': viewGithub,
  '/collab': viewCollab,
  '/ci': viewIntegrations,
  '/deployments': viewDeployments,
  '/reviews': viewIntegrations,
  '/review-gate': viewReviewGate,
  '/topology': viewTopology,
  '/architecture': viewArchitecture,
  '/missions': viewMissions,
  '/missions/:id': viewMission,
  '/missions/:id/tests': viewMissionTests,
  '/missions/:id/reviews': viewMissionReviews,
  '/herd': viewSessions,
  '/herd/session/:id': viewSession,
  '/services': viewServices,
  '/system-services': viewServices,
  '/local-services': viewServices,
  '/fabric-services': viewFabricServices,
  '/services/:id': viewService,
  '/hosts': viewHosts,
  '/personal-assistant': viewPersonalAssistant,
  '/intelligence': viewIntelligence,
  '/hosts/:id': viewHost,
  '/activity': viewActivity,
  '/analytics': viewAnalytics,
  '/analytics/missions': viewAnalytics,
  '/analytics/models': viewAnalytics,
  '/analytics/hosts': viewAnalytics,
  '/analytics/tokens': viewAnalytics,
  '/analytics/stages': viewAnalytics,
  '/analytics/usage': viewAnalytics,
  '/llm': viewLlm,
  '/llm/session/:id': viewLlmSession,
  '/artifacts': viewArtifacts,
  '/artifacts/:id': viewArtifact,
  '/publications': viewPublications,
  '/scheduling': viewScheduling,
  '/handoffs': viewHandoffs,
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

async function viewPersonalAssistant() {
  root.innerHTML = `${liveHead('<h1>Personal Assistant</h1>')}<div id="personal-assistant"></div>`;
  const chip = root.querySelector('herdr-freshness');
  return { dispose: mountPersonalAssistant(root.querySelector('#personal-assistant'), client,
    { onFreshness: (ok, message) => chip.touch(ok, message) }) };
}

async function viewIntelligence() {
  root.innerHTML = `${liveHead('<h1>Project intelligence</h1>')}<div id="project-intelligence"></div>`;
  const chip = root.querySelector('herdr-freshness');
  const view = mountProjectIntelligence(root.querySelector('#project-intelligence'), client, { query: route().query,
    onFreshness: (ok, message) => chip.touch(ok, message),
    setQuery: (o) => history.replaceState(null, '', `#/intelligence${Object.keys(o).length ? `?${new URLSearchParams(o)}` : ''}`) });
  return { dispose: view.dispose, refresh: view.refresh };
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
  const startedAt = Date.now();
  unmountView();
  if (sessionRequest) { sessionRequest.abort(); sessionRequest = null; }
  const { parts } = route();
  const match = matchRoute(parts);
  const load = (rev) => ROUTES[match](parts, rev);
  // As before, live events retry a failed or not-found page, except pages that stream their own detail.
  const streamsOwnDetail = match === '/herd/session/:id' || (match === '/missions/:id' && (safeDecode(parts[1]) || '').startsWith('pi:'));
  root.innerHTML = routeSkeleton(match, parts);
  let view;
  try {
    view = await load(revision);
  } catch (err) {
    if (revision !== renderRevision) return;
    const detail = classifyDashboardError(err, Date.now() - startedAt);
    statusPage(`<herdr-error message="${esc(err.message || dashboardErrorMessage('This page', detail))}"></herdr-error><p>${esc(dashboardErrorMessage('This page', detail))} <button type="button" data-route-retry>Retry</button>${err.requestId ? ` Request ${esc(err.requestId)}` : ''}</p>`, false, err.message || 'request failed');
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
  const back = match.startsWith('/missions/:id') || match === '/herd/session/:id' ? BACK_LINK_SKELETON : match === '/llm/session/:id' ? '<p style="font-size:13px"><span class="hd-skeleton hd-skeleton--line" style="display:inline-block;vertical-align:middle">&nbsp;</span></p>' : '';
  const piMission = match === '/missions/:id' && (safeDecode(parts[1]) || '').startsWith('pi:');
  const rows = piMission ? 1 : match === '/missions/:id/tests' || match === '/analytics/models' || match === '/analytics/hosts' || match === '/deployments' ? 2 : match === '/missions/:id/reviews' ? 4 : match === '/missions/:id' || match.startsWith('/llm') || match === '/herd/session/:id' || match === '/analytics' || match === '/analytics/missions' ? 8 : match.startsWith('/services') || match.endsWith('-services') ? 7 : 5;
  return `<div data-skeleton aria-busy="true" role="status" aria-label="Loading page">${back}<div class="hd-live-head"><div class="hd-skeleton hd-skeleton--title"></div></div>${skeleton('page content', { rows, height: 64, bare: true })}</div>`;
}

// ------------------------------------------------------------- views
async function viewArtifacts() {
  root.innerHTML = `${liveHead('<h1>Artifacts</h1>')}<div id="artifact-registry"></div>`;
  return mountArtifacts(root.querySelector('#artifact-registry'), client, { freshness: chipIn(root) });
}

async function viewArtifact(parts, revision) {
  const id = decodeURIComponent(parts[1]);
  root.innerHTML = `<a href="#/artifacts">← Artifacts</a>${liveHead('<h1>Artifact</h1>')}<div id="artifact-detail">${skeleton('artifact', { rows: 8, height: 64 })}</div>`;
  const container = root.querySelector('#artifact-detail');
  const controller = new AbortController();
  const chip = root.querySelector('herdr-freshness');
  // Mount immediately so navigation can cancel the pending detail read.
  void renderArtifactDetail(container, client, id, { signal: controller.signal, versionId: route().query.get('version') || undefined })
    .then(result => { if (!controller.signal.aborted && revision === renderRevision) chip.touch(!result?.errors?.length, result?.errors?.join('; ')); })
    .catch(error => { if (!controller.signal.aborted && revision === renderRevision) { container.innerHTML = `<herdr-error message="${esc(error.message)}"></herdr-error><button type="button" data-route-retry>Retry</button>`; chip.touch(false, error.message); } });
  return { refresh() {}, dispose: () => controller.abort() };
}

async function viewPublications() {
  root.innerHTML = `${liveHead('<h1>Publications</h1>')}<div id="publication-registry"></div>`;
  return mountPublications(root.querySelector('#publication-registry'), client, { freshness: chipIn(root) });
}

async function viewScheduling() {
  root.innerHTML = `${liveHead('<h1>Scheduling</h1>')}<div id="scheduler"></div>`;
  return mountScheduling(root.querySelector('#scheduler'), client, { freshness: chipIn(root) });
}

async function viewDeployments() {
  root.innerHTML = `${liveHead('<h1>Deployments</h1>')}<div id="deployments"></div>`;
  return mountDeployments(root.querySelector('#deployments'), client, { freshness: chipIn(root) });
}

async function viewHandoffs() {
  root.innerHTML = `${liveHead('<h1>Handoffs</h1>')}<div id="handoffs"></div>`;
  return mountHandoffs(root.querySelector('#handoffs'), client, { freshness: chipIn(root) });
}

// ------------------------------------------------------------- topology
async function viewTopology() {
  return ownPolling(mountTopology(root, client, {status: route().query.get('status') || 'current'}));
}

async function viewArchitecture() {
  const q = route().query;
  root.innerHTML = `${liveHead('')}<div id="architecture-root"></div>`;
  const chip = root.querySelector('herdr-freshness');
  return ownPolling(mountArchitecture(root.querySelector('#architecture-root'), client, {snapshot: q.get('snapshot') || '', parent: q.get('parent') || '', q: q.get('q') || '', onFreshness: (ok, message) => chip.touch(ok, message)}));
}

async function viewOverview() {
  root.innerHTML = `${liveHead('<h1>Overview</h1>')}<div id="overview-dashboard"></div>`;
  return ownPolling(mountOverview(document.getElementById('overview-dashboard'), client, { freshness: chipIn(root) }));
}

async function viewAttention() {
  root.innerHTML = `${liveHead('')}<div id="attention-root"></div>`;
  const chip = root.querySelector('herdr-freshness');
  return ownPolling(mountAttention(root.querySelector('#attention-root'), client, { onBody: handleAttention, notifier: attentionNotifier, onFreshness: (ok, message) => chip.touch(ok, message) }));
}

async function viewCollab() {
  // The chip sits outside the collab root: collab redraws its whole frame on every page change.
  root.innerHTML = `${liveHead('', 30000)}<div id="collab-root">${skeleton('collaboration', { rows: 8, height: 64 })}</div>`;
  const chip = chipIn(root);
  return ownPolling(mountCollab(document.getElementById('collab-root'), {
    fabricSessions: () => client.listSessions('active'),
    onFreshness: (ok, message) => chip()?.touch(ok, message),
  }));
}

async function viewGithub() {
  return ownPolling(mountGithub(root, client));
}

async function viewGithubProjects() {
  // Read-only Projects v2 boards; keeps its own 5 s poll, live events refresh it too.
  const query = route().query;
  return mountGithubProjects(root, client, { project: query.get('project') || '', layout: query.get('layout') || 'board' });
}

async function viewReviewGate() {
  root.innerHTML = `${liveHead('')}<div id="review-gate-root"></div>`;
  const chip = root.querySelector('herdr-freshness');
  return ownPolling(mountReviewGate(root.querySelector('#review-gate-root'), client, { onFreshness: (ok, message) => chip.touch(ok, message) }));
}

async function viewIntegrations(parts) {
  const query = route().query;
  return ownPolling(mountIntegrations(root, client, {
    kind: parts[0], tab: query.get('tab') || 'current', search: query.get('search') || '', workspace: query.get('workspace') || '',
    repository: query.get('repository') || '', provider: query.get('provider') || '', mission: query.get('mission') || '', session: query.get('session') || '',
  }));
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

// The Related panel replaces the ad-hoc link rows; the filter links stay as its footer.
// Returns the panel's disposer; the calling view folds it into its own dispose().
function showRelated(kind, id, host, footer = '', extra = {}) {
  const el = host || document.getElementById('related-panel');
  if (!el) return () => {};
  const dispose = mountRelated(el, client, kind, id, { footer, ...extra });
  return typeof dispose === 'function' ? dispose : () => {};
}

// Runs every disposer once, even if one throws.
function combineDisposers(...disposers) {
  return () => { for (const d of disposers) { try { d?.(); } catch { /* the rest still run */ } } };
}

// A malformed %-escape in the hash is a not-found page, never an uncaught rejection.
function safeDecode(value) {
  try { return decodeURIComponent(value ?? ''); } catch { return null; }
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
    root.innerHTML = `<a href="#/missions">← Missions</a>${liveHead('<h1>Pi mission</h1>')}<div id="related-panel"></div><div id="live-mission-detail"></div>`;
    const relatedDispose = showRelated('mission', key, null, relatedIntegrationLinks({},'mission',key));
    const disposeActivity = mountMissionActivity(document.getElementById('live-mission-detail'), client, key, { onStatus });
    return { refresh() {}, dispose: combineDisposers(relatedDispose, disposeActivity) };
  }
  if (!m) { statusPage('<herdr-error message="Mission not found"></herdr-error>', true); return; }
  root.innerHTML = `<a href="#/missions">← Missions</a>${liveHead(`<h1 style="overflow-wrap:anywhere">${key.startsWith('pi:') ? 'Pi mission' : esc((m.title || 'Mission').slice(0, 180))}</h1>`)}${missionSectionNav(key)}${key.startsWith('pi:') ? '<div id="live-mission-detail"></div>' : ''}<div id="mission-story"></div><div id="related-panel"></div><div id="mission-history"></div>`;
  // The story (what happened, how it ended, what needs you) sits above the recorded history; when it is unavailable the page is as before.
  const story = mountMissionStory(document.getElementById('mission-story'), client, key);
  const relatedDispose = showRelated('mission', key, null, relatedIntegrationLinks(m,'mission',key));
  const disposeLive = key.startsWith('pi:') ? mountMissionActivity(document.getElementById('live-mission-detail'), client, key, { onStatus }) : null;
  let recordedEvents = null;
  const disposeMission = combineDisposers(story?.dispose, relatedDispose, disposeLive, () => recordedEvents?.dispose());
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
      const sessionsHtml = await missionSessions(m);
      const streamHtml = recordedEvents?.html() || '<p class="hd-card__sub">Loading recorded events…</p>';
      if (revision !== renderRevision || !history.isConnected) return;
      // Morphed in place: identical data writes nothing and open disclosures stay open.
      morph(history, `<h2>Recorded history</h2>
    <div style="margin:8px 0"><herdr-status stage="${esc(m.status)}"></herdr-status>
      <span class="hd-card__sub"> · ${sessions} agents · ${esc(m.stage || 'Stage not recorded')} · created ${esc(formatTimestamp(m.created_at))} · last reported change ${esc(formatTimestamp(m.updated_at))}</span></div>
    <div style="margin:6px 0">${telemetryBadge(m.telemetry_state)}
      <herdr-metric label="Stall age" value="${esc(stallAge(m))}" hint="${esc(stallHint(m))}"></herdr-metric></div>
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

      if (!recordedEvents) {
        recordedEvents = mountEventHistory(history.querySelector('#mission-stream'), client,
          'mission', missionKey(m), {missionTitles,
            onStatus: (ok, message) => ok ? sources.ok('events') : sources.fail('events', message)});
      } else void recordedEvents.refresh();

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
  try { await refreshHistory(m); } catch (error) { disposeMission(); throw error; }
  if (revision !== renderRevision) { disposeMission(); return; }
  // Mission telemetry owns its polling; a live event refreshes only the recorded history, in place.
  return { refresh: () => refreshHistory(), dispose: disposeMission };
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

// A delivery run's own status text. Test runs / builds report statuses like
// passed/failed/ok/error; these are shown as text (never color alone) with a
// semantic <herdr-status> for the recognized lifecycle values.
function runStatusText(run) {
  const raw = typeof run.status === 'string' ? run.status : '';
  if (/^pass|^ok|^success|^healthy/.test(raw)) return { label: 'Passed', tone: 'healthy' };
  if (/^fail|^error|^crash/.test(raw)) return { label: 'Failed', tone: 'failed' };
  if (raw) return { label: raw, tone: raw.toLowerCase() };
  return { label: 'Unknown', tone: 'unknown' };
}

function runDuration(run) {
  if (!run.started_at) return null;
  const start = Date.parse(run.started_at);
  if (Number.isNaN(start)) return null;
  // Only a run that is still in progress uses the current clock; a run without
  // an end timestamp that is not running has no trustworthy duration. Never
  // fabricate a duration for a completed run with a missing end.
  const running = /^(running|in progress|in_progress|active|queued|pending)$/.test((run.status || '').toLowerCase());
  const end = run.ended_at ? Date.parse(run.ended_at) : (running ? Date.now() : NaN);
  if (Number.isNaN(end)) return null;
  const ms = end - start;
  return ms >= 0 ? formatDuration(ms) : null;
}

// A read-only telemetry-state badge (live/stale/unknown) from the mission
// record's ``telemetry_state`` (issue #28). Text first, never colour alone:
// UNKNOWN is its own word so a mission with no collector is never mistaken for
// live work.
function telemetryBadge(state) {
  const s = typeof state === 'string' ? state.toLowerCase() : '';
  const label = s === 'live' ? 'Live telemetry' : s === 'stale' ? 'Stale telemetry' : 'Telemetry unknown';
  // Text first, never colour alone. A dedicated element (not herdr-status) so it
  // can't be mistaken for, or collide with, the mission's own status badge.
  return `<span class="hd-card__sub" data-telemetry-state>${esc(label)}</span>`;
}

// Stall age = the age of the most recent activity report (collector freshness),
// exposed by the server as report_age_seconds from the same activity store that
// derives telemetry_state (issue #28). For a no-collector mission (telemetry
// state unknown) report_age_seconds is null and we say so rather than imply a
// liveness reading from an absent timestamp or the lifecycle record's updated_at.
function stallAge(m) {
  const age = m?.report_age_seconds;
  if (typeof age !== 'number') return 'Unknown';  // no collector / no freshness data
  return formatDuration(age * 1000);
}

function stallHint(m) {
  const s = typeof m?.telemetry_state === 'string' ? m.telemetry_state.toLowerCase() : '';
  if (typeof m?.report_age_seconds !== 'number') return 'No collector attached for this mission';
  if (s === 'stale') return 'Collector connected but has not reported recently';
  if (s === 'live') return 'Collector reporting as expected';
  return 'Since the most recent activity report';
}

function missionSectionNav(key) {
  const e = (s) => encodeURIComponent(s);
  return `<nav class="hd-inline" aria-label="Mission sections" style="margin:14px 0;font-size:13px">`
    + `<a href="#/missions/${e(key)}">Mission</a>`
    + `<a href="#/missions/${e(key)}/tests">Tests & builds</a>`
    + `<a href="#/missions/${e(key)}/reviews">Reviews & PRs</a>`
    + `</nav>`;
}

function renderDeliveryRuns(runs) {
  if (!runs.length) return '<herdr-empty message="No tests or builds recorded for this mission"></herdr-empty>';
  return runs.map((run) => {
    const st = runStatusText(run);
    const counts = ['passed', 'failed', 'skipped', 'total']
      .filter((c) => run[c] != null)
      .map((c) => `${esc(c)} ${esc(String(run[c]))}`)
      .join(' · ');
    const duration = runDuration(run);
    const kind = run.isBuild ? 'Build' : 'Test run';
    return `<div class="hd-card" style="margin:8px 0">`
      + `<div class="hd-card__title">${kind} · <herdr-status stage="${esc(st.tone)}"></herdr-status> <span class="hd-card__sub">${esc(st.label)}</span></div>`
      + (counts ? `<div class="hd-card__sub">${counts}</div>` : `<div class="hd-card__sub">Counts unknown</div>`)
      + `<div class="hd-card__sub">${duration ? `Duration ${esc(duration)}` : 'Duration not recorded'}${run.started_at ? ` · started ${esc(formatTimestamp(run.started_at))}` : ''}</div>`
      + `</div>`;
  }).join('');
}

async function viewMissionTests(parts, revision) {
  const key = decodeURIComponent(parts[1]);
  let mission = null;
  try { mission = await client.getMission(key); } catch (error) { if (error.status !== 404) throw error; }
  if (revision !== renderRevision) return;
  if (!mission) {
    statusPage(`<a href="#/missions">← Missions</a><herdr-error message="Mission not found"></herdr-error>`, true);
    return;
  }
  const title = (mission.title || 'Mission').slice(0, 180);
  root.innerHTML = `<a href="#/missions">← Missions</a>${liveHead(`<h1 style="overflow-wrap:anywhere">${esc(title)} · Tests & builds</h1>`)}${missionSectionNav(key)}<div id="mission-tests"></div>`;
  const host = document.getElementById('mission-tests');
  let pending = false;
  let trailing = false;
  const loadRuns = async (initial) => {
    if (pending) { trailing = true; return; }
    if (!host.isConnected || document.hidden) return;
    pending = true;
    try {
      // Page-specific loading state: the section is blank until its first data
      // arrives, so show a spinner rather than an empty flash.
      if (initial) host.innerHTML = skeleton('tests & builds');
      let tests = [], builds = [];
      const [t, b] = await Promise.all([client.missionTests(key), client.missionBuilds(key)]);
      tests = (t && t.tests) || []; builds = (b && b.builds) || [];
      if (revision !== renderRevision || !host.isConnected) return;
      const runs = [...builds.map((r) => ({ ...r, isBuild: true })), ...tests.map((r) => ({ ...r, isBuild: false }))]
        .sort((a, b) => (b.started_at || '').localeCompare(a.started_at || ''));
      if (initial) host.innerHTML = renderDeliveryRuns(runs);
      else morph(host, renderDeliveryRuns(runs));
      root.querySelector('herdr-freshness')?.touch(true);
    } catch (error) {
      if (!host.isConnected) return;
      root.querySelector('herdr-freshness')?.touch(false, error.message);
      if (initial) host.innerHTML = `<herdr-error message="${esc(error.message)}"></herdr-error>`;
    } finally {
      pending = false;
      if (trailing) { trailing = false; loadRuns(false); }
    }
  };
  try { await loadRuns(true); } catch { /* not-found handled below */ }
  if (revision !== renderRevision) return;
  return { refresh: () => loadRuns(false), dispose() {} };
}

// A review's verdict text and a pull request's merge status, shown as text
// (never color alone). UNKNOWN verdicts render 'unknown', never approved.
function reviewVerdictText(verdict) {
  const v = typeof verdict === 'string' ? verdict.trim().toLowerCase() : '';
  if (!v) return { label: 'Unknown', tone: 'unknown' };
  if (v === 'unknown') return { label: 'Unknown', tone: 'unknown' };
  if (v === 'approved' || v === 'approve') return { label: 'Approved', tone: 'healthy' };
  if (v.includes('changes') || v === 'request_changes' || v === 'rejected' || v === 'denied') return { label: 'Changes requested', tone: 'attention' };
  if (v === 'commented' || v === 'comment') return { label: 'Commented', tone: 'waiting' };
  return { label: verdict, tone: v };
}

function prStatusText(status) {
  const s = typeof status === 'string' ? status.trim().toLowerCase() : '';
  if (s === 'merged') return { label: 'Merged', tone: 'healthy' };
  if (s === 'open' || s === 'opened') return { label: 'Open', tone: 'active' };
  if (s === 'closed' || s === 'rejected') return { label: 'Closed', tone: 'waiting' };
  return { label: status || 'Unknown', tone: s || 'unknown' };
}

function renderReviewsAndPulls(reviews, pulls) {
  const sections = [];
  sections.push(`<h2>Reviews</h2>`);
  if (!reviews.length) sections.push('<herdr-empty message="No reviews recorded for this mission"></herdr-empty>');
  sections.push(reviews.map((review) => {
    const v = reviewVerdictText(review.verdict);
    const findings = (review.findings || [])
      .map((f) => `<li>${esc(f.severity || 'info')}: ${esc(f.summary || '')}${f.detail ? ` — ${esc(f.detail)}` : ''}</li>`)
      .join('');
    return `<div class="hd-card" style="margin:8px 0">`
      + `<div class="hd-card__title"><herdr-status stage="${esc(v.tone)}"></herdr-status> <span>${esc(v.label)}</span></div>`
      + `<div class="hd-card__sub">${review.reviewer ? `Reviewed by ${esc(review.reviewer)}` : 'Reviewer not recorded'}${review.completed_at ? ` · ${esc(formatTimestamp(review.completed_at))}` : ''}</div>`
      + (findings ? `<ul style="margin:6px 0 0;padding-left:18px">${findings}</ul>` : '')
      + `</div>`;
  }).join(''));
  sections.push(`<h2>Pull requests</h2>`);
  if (!pulls.length) sections.push('<herdr-empty message="No pull requests recorded for this mission"></herdr-empty>');
  sections.push(pulls.map((pr) => {
    const st = prStatusText(pr.status);
    const branch = pr.branch ? `${esc(pr.branch)} → ${esc(pr.base || '?')}` : '';
    const link = typeof pr.url === 'string' && /^https?:\/\//.test(pr.url)
      ? `<a href="${esc(pr.url)}" target="_blank" rel="noopener noreferrer">PR #${esc(String(pr.number))}</a>`
      : `PR #${esc(String(pr.number))}`;
    return `<div class="hd-card" style="margin:8px 0">`
      + `<div class="hd-card__title"><herdr-status stage="${esc(st.tone)}"></herdr-status> ${link}</div>`
      + `<div class="hd-card__sub"><span>${esc(st.label)}</span>${branch ? ` · ${branch}` : ''}${pr.repository ? ` · ${esc(pr.repository)}` : ''}</div>`
      + `</div>`;
  }).join(''));
  return sections.join('');
}

async function viewMissionReviews(parts, revision) {
  const key = decodeURIComponent(parts[1]);
  let mission;
  try { mission = await client.getMission(key); } catch (error) { if (error.status !== 404) throw error; }
  if (revision !== renderRevision) return;
  if (!mission) {
    statusPage(`<a href="#/missions">← Missions</a><herdr-error message="Mission not found"></herdr-error>`, true);
    return;
  }
  const title = (mission.title || 'Mission').slice(0, 180);
  root.innerHTML = `<a href="#/missions">← Missions</a>${liveHead(`<h1 style="overflow-wrap:anywhere">${esc(title)} · Reviews & PRs</h1>`)}${missionSectionNav(key)}<div id="mission-reviews"></div>`;
  const host = document.getElementById('mission-reviews');
  let pending = false;
  let trailing = false;
  const loadReviews = async (initial) => {
    if (pending) { trailing = true; return; }
    if (!host.isConnected || document.hidden) return;
    pending = true;
    try {
      if (initial) host.innerHTML = '<herdr-spinner label="Loading reviews & PRs"></herdr-spinner>';
      let reviews = [], pulls = [];
      const [rv, pr] = await Promise.all([client.missionReviews(key), client.missionPulls(key)]);
      reviews = (rv && rv.reviews) || []; pulls = (pr && pr.pulls) || [];
      if (revision !== renderRevision || !host.isConnected) return;
      if (initial) host.innerHTML = renderReviewsAndPulls(reviews, pulls);
      else morph(host, renderReviewsAndPulls(reviews, pulls));
      root.querySelector('herdr-freshness')?.touch(true);
    } catch (error) {
      if (!host.isConnected) return;
      root.querySelector('herdr-freshness')?.touch(false, error.message);
      if (initial) host.innerHTML = `<herdr-error message="${esc(error.message)}"></herdr-error>`;
    } finally {
      pending = false;
      if (trailing) { trailing = false; loadReviews(false); }
    }
  };
  try { await loadReviews(true); } catch { /* handled inside */ }
  if (revision !== renderRevision) return;
  return { refresh: () => loadReviews(false), dispose() {} };
}

async function viewSessions() {
  return ownPolling(mountSessionsView(root, client, {tab: route().query.get('tab') || 'current'}));
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
       ${terminal ? `<p class="sd-meta">${outcome === 'failed' ? 'Failed' : 'Finished'} agent · recorded inspection remains available.</p>` : ''}
       <div id="session-story"></div>
      ${s.mission_id || s.mission_key ? `<p class="sd-meta" id="session-mission">Mission association not resolved · ${esc(s.mission_key || s.mission_id)}</p>` : ''}
      <div id="related-panel"></div>
      <div id="session-terminal" class="sd-details"></div>
      <details class="hd-card sd-details" id="session-details"><summary>Agent details</summary><dl>${fields.map(([label,value])=>`<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`).join('')}</dl>${s.summary ? `<p>${esc(s.summary)}</p>` : ''}</details>
      <div id="session-workspace-host"></div>
      <details class="hd-card sd-details" id="advanced-inspector"><summary>Advanced inspection</summary><div id="inspector-host"></div></details>
      <details class="hd-card sd-details" id="session-events"><summary>Recorded event stream</summary><div id="session-stream">${skeleton('recorded events', { rows: 2 })}</div></details>`;
    // The story shows how the agent began and any handoff in or out; without handoff data it is the start and the last report.
    const storyHost = document.getElementById('session-story');
    const drawStory = tree => { if (storyHost.isConnected) storyHost.innerHTML = renderStory(agentStory(s, tree)); };
    drawStory({});
    client.getHandoffTree?.({signal: controller.signal}).then(drawStory).catch(() => { /* the story keeps start and last report */ });
    const relatedDispose = showRelated('session', s.session_id, null, relatedIntegrationLinks(s,'session',s.session_id), { sessionId: s.session_id });
    const disposeWorkspace = mountSessionWorkspace(document.getElementById('session-workspace-host'), client, s, {onRename: profile => { s.display_name = profile.display_name; const title = root.querySelector('.sd-title'); if (title) title.textContent = `${profile.display_name}${project ? ` · ${clip(project)}` : ''}`; }});
    let disposeAdvanced = null;
    const advanced = document.getElementById('advanced-inspector');
    advanced.addEventListener('toggle', () => {
      if (advanced.open && !disposeAdvanced) disposeAdvanced = mountInspector(document.getElementById('inspector-host'), client, s.session_id);
      else if (!advanced.open && disposeAdvanced) { disposeAdvanced(); disposeAdvanced = null; document.getElementById('inspector-host').replaceChildren(); }
    });
    const disposeTerminal = mountTerminalAttach(document.getElementById('session-terminal'), client, s, {copyText});
    const disposeSession = combineDisposers(relatedDispose, disposeWorkspace, disposeTerminal, () => disposeAdvanced?.());
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
    const recordedEvents = mountEventHistory(stream, client, 'session', s.session_id,
      {missionTitles, onStatus: (ok, message) => ok ? sources.ok('events') : sources.fail('events', message)});
    // The page streams its own detail; a live event refreshes the recorded event list (new events
    // are appended) and the head status line, in place.
    return {
      async refresh() {
        if (!stream.isConnected) return;
        headRequest?.abort();
        const request = new AbortController(); headRequest = request;
        const failed = (source, label) => (error) => { if (!request.signal.aborted) sources.fail(source, `${label}: ${error?.message || 'request failed'}`); return null; };
        void recordedEvents.refresh();
        const latest = await client.getSession(id, { signal: request.signal }).catch(failed('agent', 'Agent'));
        if (headRequest === request) headRequest = null;
        if (!stream.isConnected || request.signal.aborted) return;
        if (latest) sources.ok('agent');
        if (!latest || !head.isConnected) return;
        // A heartbeat moves the report time only; the head flashes when the status itself changes.
        patchText(head.querySelector('[data-session-updated]'), formatTimestamp(latest.updated_at));
        if (patchText(head.querySelector('[data-session-status]'), sessionStatusText(latest))) flash(head);
      },
      dispose() { headRequest?.abort(); recordedEvents.dispose(); disposeSession(); },
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

async function viewFabricServices(_parts, revision) {
  const data = await client.listFabricServices();
  if (revision !== renderRevision) return;
  return mountFabricServices(root, client, data, { search: route().query.get('search') || '' });
}

async function viewService(parts, revision) {
  const svc = await client.request(`/api/v1/services/${encodeURIComponent(decodeURIComponent(parts[1]))}`);
  if (revision !== renderRevision) return;
  if (!svc || svc.error) { root.innerHTML = '<herdr-error message="Service not found"></herdr-error>'; return; }
  // The details view rewrites its own container, so it gets a child; the Related panel sits beside it.
  root.innerHTML = '<div data-service-details></div><div id="related-panel"></div>';
  const details = mountServiceDetails(root.querySelector('[data-service-details]'), client, svc);
  const relatedDispose = showRelated('service', svc.service_id || safeDecode(parts[1]) || '', root.querySelector('#related-panel'));
  return { refresh() {}, dispose: combineDisposers(() => details.dispose(), relatedDispose) };
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
  const view = mountHostDetail(root, client, id, range, { data });
  // mountHostDetail morphs only its own body, so a slot appended after it survives refreshes.
  const slot = document.createElement('div'); slot.id = 'related-panel'; root.appendChild(slot);
  const relatedDispose = showRelated('host', safeDecode(parts[1]) || '', slot);
  return { refresh: (hint) => view.refresh(hint), dispose: combineDisposers(() => view.dispose(), relatedDispose) };
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

// The LLM pages fetch ~10 series per render and hold filter focus: they are snapshots, refreshed
// only by navigation (a filter change rewrites the hash), never by live events.
function addSnapshotChip() {
  const title = root.querySelector('h1');
  const head = document.createElement('div'); head.className = 'hd-live-head';
  if (title) { title.before(head); head.append(title); } else root.prepend(head);
  head.insertAdjacentHTML('beforeend', CHIP());
  head.querySelector('herdr-freshness').touch(true);
}

async function viewLlm(_parts, revision) {
  const q = route().query;
  const window_ = WINDOWS.includes(q.get('window')) ? q.get('window') : '24h';
  const filters = { project: q.get('project') || '', model: q.get('model') || '', mission: q.get('mission') || '' };
  const f = { window: window_, ...filters };
  const step = STEP_FOR[window_];
  const series = (metric) => client.llm('timeseries', { ...f, step, metric });
  const [total, models, agents, problems, projects, requests, errors, ttft, speed, sessions, fleet, previous, missions, queue, handoffs] = await Promise.all([
    client.llm('stats', { ...f, group: 'all' }),
    client.llm('stats', { window: window_, project: filters.project, group: 'model' }),
    client.llm('stats', { ...f, group: 'session' }),
    client.llm('errors', f),
    client.llm('stats', { window: window_, group: 'project' }),
    series('requests'), series('errors'), series('ttft_p95'), series('speed_p50'),
    client.listSessions().catch(() => ({ sessions: [] })),
    loadAnalytics(client, '').catch(() => null),
    client.llm('stats', { ...f, group: 'all', previous: 1 }).catch(() => null),
    client.llm('stats', { window: window_, project: filters.project, group: 'mission' }).catch(() => null),
    series('queue_p95').catch(() => null),
    client.listHandoffs().catch(() => null),
  ]);
  if (revision !== renderRevision) return NO_REFRESH;
  const names = {};
  for (const s of sessions.sessions || []) if (s.display_name) names[s.session_id] = s.display_name;
  root.innerHTML = renderLlm({
    window: window_, filters, names,
    projects: projects.groups.map((g) => g.key),
    modelNames: [...new Set(models.groups.map((g) => g.key))],
    missionNames: (missions?.groups || []).map((g) => g.key),
  }, { total, models, agents, problems, previous, missions, handoffs: handoffs?.handoffs, series: { requests, errors, ttft, speed, queue } });
  if (fleet) root.insertAdjacentHTML('beforeend', usageStrip(fleet));
  addSnapshotChip();
  root.querySelectorAll('[data-llm-filter]').forEach((el) => el.addEventListener('change', () => {
    window.location.hash = `#/llm?${llmQuery({ window: window_, ...filters, [el.dataset.llmFilter]: el.value })}`;
  }));
  return NO_REFRESH;
}
async function viewLlmSession(parts, revision) {
  const id = safeDecode(parts[2]);
  if (id == null) { root.innerHTML = '<herdr-error message="Agent not found"></herdr-error>'; return NO_REFRESH; }
  const q = route().query;
  const window_ = WINDOWS.includes(q.get('window')) ? q.get('window') : '24h';
  const f = { window: window_, session: id };
  const step = STEP_FOR[window_];
  const series = (metric) => client.llm('timeseries', { ...f, step, metric });
  const [total, models, problems, requests, errors, ttft, speed, session, previous, queue, handoffs, agents, sessions] = await Promise.all([
    client.llm('stats', { ...f, group: 'all' }), client.llm('stats', { ...f, group: 'model' }),
    client.llm('errors', f), series('requests'), series('errors'), series('ttft_p95'), series('speed_p50'),
    client.getSession(id).catch(() => null),
    client.llm('stats', { ...f, group: 'all', previous: 1 }).catch(() => null),
    series('queue_p95').catch(() => null),
    client.listHandoffs().catch(() => null),
    client.llm('stats', { window: window_, group: 'session' }).catch(() => null),
    client.listSessions().catch(() => ({ sessions: [] })),
  ]);
  const names = {};
  for (const s of sessions.sessions || []) if (s.display_name) names[s.session_id] = s.display_name;
  if (revision !== renderRevision) return NO_REFRESH;
  root.innerHTML = renderLlmSession({
    window: window_, sessionId: id, known: Boolean(session), name: session?.display_name || session?.agent_role || '', names,
  }, { total, models, problems, previous, handoffs: handoffs?.handoffs, agents, series: { requests, errors, ttft, speed, queue } });
  addSnapshotChip();
  if (session) {
    const slot = document.createElement('div'); slot.id = 'related-panel';
    root.querySelector('.hd-live-head').after(slot);
    return { refresh() {}, dispose: showRelated('session', id, slot, '', { compact: true }) };
  }
  return NO_REFRESH;
}


// Live updates (Spec §52, §85): SSE events feed the refresh bus, which calls
// the current view's refresh() at most once per LIVE_REFRESH_MS (first event
// after idle at once, the rest coalesced to the window's end). A hidden tab
// pauses the bus; becoming visible refreshes the view once.
connectEvents({
  url: '/api/v1/events/stream',
  onEvent: (event) => bus.dispatch(event),
  onContact: () => noteStreamContact(true),
  onError: () => { noteStreamContact(false); /* keep last view; the client auto-reconnects */ },
});

document.addEventListener('visibilitychange', () => { if (document.hidden) bus.pause(); else bus.resume(); });
if (document.hidden) bus.pause();

window.addEventListener('hashchange', () => render());
root.addEventListener('click', event => { if (event.target.closest?.('[data-route-retry]')) render(); });
completeSignInIfRedirected().finally(() => render());
