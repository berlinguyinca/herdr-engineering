// The dashboard app (packages/index.html) under the visual harness, WITHOUT a control-plane process.
//
// The collaboration surfaces are captured over a real server fixture (real_server.py). The Missions page and the Personal Assistant
// tabs live in the dashboard app instead, which only needs a static file server over packages/ and the /api/v1 routes it calls. So
// the harness serves packages/ itself and answers /api/v1 from the deterministic fixtures below (page.route, like the live-view
// tests in helpers/live.mjs): no network, no server process, fixed ids and timestamps. The browser clock is fixed by capture()
// (page.clock.setFixedTime), so every "age" the pages print is computed against CFG.screenshots.fixed_clock.
//
// A surface opts in with `app: 'dashboard'`, a `hash` (the route) and optional `steps`. visual-baselines.mjs capture() calls
// openDashboard() for it instead of openReal().
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { CFG, REPO } from './ui-gates-lib.mjs';

const PACKAGES = path.join(REPO, 'packages');
const TYPES = { '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.json': 'application/json', '.svg': 'image/svg+xml', '.md': 'text/markdown', '.woff2': 'font/woff2' };

let serverPromise = null;
/** One static server for the whole process, unref'd so it never keeps a finished run alive. */
export function dashboardOrigin() {
  serverPromise ||= new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      const url = new URL(req.url, 'http://x');
      const file = path.resolve(PACKAGES, '.' + decodeURIComponent(url.pathname === '/' ? '/index.html' : url.pathname));
      if (!file.startsWith(PACKAGES + path.sep) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.writeHead(404); return res.end('not found'); }
      res.writeHead(200, { 'content-type': TYPES[path.extname(file)] || 'application/octet-stream' });
      fs.createReadStream(file).pipe(res);
    });
    server.listen(0, '127.0.0.1', () => { server.unref(); resolve(`http://127.0.0.1:${server.address().port}`); });
  });
  return serverPromise;
}

// ---- the fixed instant every fixture is relative to ------------------------------------------------------------------------
const NOW = Date.parse(CFG.screenshots.fixed_clock);
const ago = (seconds) => new Date(NOW - seconds * 1000).toISOString().replace('.000Z', 'Z');

// ---- Missions --------------------------------------------------------------------------------------------------------------
function projection(key, { title, status = 'EXECUTING', summary, health = 'active', host = 'beast', reportAge = 2, coverage, waiting, workers = [], output = 20 }) {
  const id = key.split(':')[1];
  return {
    contract_version: 1, mission_key: key, host_id: host, observed_at: ago(reportAge), snapshot_at: ago(reportAge), activity_snapshot_at: ago(reportAge), connection: 'connected',
    mission: { id, title, status },
    freshness: { state: 'live', age_seconds: reportAge, stale_after_seconds: 3600, disconnect_after_seconds: 7200 },
    activity: { health, currentActivity: { summary }, acceptanceCoverage: coverage, lastOutputAt: ago(output), workerDetails: workers,
      ...(waiting ? { waitingReason: waiting.reason, waitingSince: ago(waiting.since) } : {}) },
  };
}
const worker = (id, state, ended = false) => ({ id, state, ...(ended ? { endedAt: ago(600) } : {}) });
const MISSION_ACTIVITY = [
  projection('pi:M-201', { title: 'Add a health endpoint to the gateway', summary: 'Running the gateway test suite', coverage: { completed: 3, total: 5 }, host: 'beast', workers: [worker('w1', 'running'), worker('w2', 'running'), worker('w3', 'complete', true)] }),
  projection('pi:M-202', { title: 'Migrate the router tests to the new fixtures', summary: 'Rewriting fixtures for the hosts page', coverage: { completed: 1, total: 4 }, host: 'bender', workers: [worker('w4', 'running')], output: 95 }),
  projection('pi:M-203', { title: 'Fix the flaky relay sticky-bias test', health: 'blocked', summary: 'Waiting for a reviewer to unblock the branch', coverage: { completed: 0, total: 3 }, host: 'beast',
    waiting: { reason: 'review_required', since: 1500 }, workers: [worker('w5', 'blocked')], output: 1500 }),
  // a report older than its freshness window: last known, never counted as running
  { ...projection('pi:M-204', { title: 'Draft the quarterly capacity note', summary: 'Collecting GPU hours per model', host: 'nibbler', reportAge: 4 * 3600, output: 4 * 3600 }),
    freshness: { state: 'stale', age_seconds: 4 * 3600, stale_after_seconds: 3600, disconnect_after_seconds: 7200 } },
];
const MISSIONS = [
  { mission_id: 'M-301', source: 'herdr', title: 'Roll out the unified gateway scheduling image', status: 'active', stage: 'implementation', purpose: 'Roll the gateway image out to zapp and bender', host_id: 'zapp', updated_at: ago(7200), last_seen_at: ago(7200) },
  { mission_id: 'M-302', source: 'herdr', title: 'Replace the CI cache layer', status: 'failed', stage: 'verification', purpose: 'Swap the cache layer for the shared one', host_id: 'bender', updated_at: ago(86400), last_seen_at: ago(86400) },
  { mission_id: 'M-303', source: 'herdr', title: 'Publish the benchmark workspace', status: 'complete', stage: 'done', purpose: 'Publish the redesigned workspace', host_id: 'beast', updated_at: ago(172800), last_seen_at: ago(172800) },
  { mission_id: 'M-304', source: 'herdr', title: 'Tidy the host telemetry labels', status: 'closed', stage: 'done', purpose: 'Rename the telemetry labels', host_id: 'nibbler', updated_at: ago(259200), last_seen_at: ago(259200) },
];
const MISSION_OUTCOMES = {
  'M-302': { result: 'failed', duration_s: 5400, agent_count: 3, repository: 'org/ci', last_event: 'verification failed: cache key mismatch' },
  'M-303': { result: 'succeeded', duration_s: 12600, agent_count: 4, repository: 'org/bench', last_event: 'pull request merged' },
  'M-304': { result: 'succeeded', duration_s: 840, agent_count: 1, repository: 'org/herdr', last_event: 'labels renamed' },
};

// ---- Personal Assistant ----------------------------------------------------------------------------------------------------
const created = '2026-10-23T09:00:00Z';
const assistantDb = () => ({
  assistants: [{ assistant_id: 'asst_1', name: 'Personal Assistant', persona: 'helpful', status: 'ready', instructions: '', model_policy: 'auto', enabled: true, created_at: created, updated_at: created }],
  connectors: [{ connector_id: 'conn_1', name: 'Weather', kind: 'http', base_url: 'https://w.example.com', auth_present: false, enabled: true }],
  approvals: [
    { approval_id: 'appr_1', assistant_id: 'asst_1', decision: 'pending', policy_snapshot: 'request_review:claude', tool_id: 'request_review', action_status: 'requested', executable: true,
      requested_by: 'key:alice', dry_run: 'Ask claude to review pr:o/r#7; records a request only', expires_at: '2026-10-23T11:00:00Z', result: {} },
    { approval_id: 'appr_2', assistant_id: 'asst_1', decision: 'approved', policy_snapshot: 'request_review:pi', tool_id: 'request_review', action_status: 'completed', executable: true,
      requested_by: 'key:alice', dry_run: 'Ask pi to review pr:o/r#5; records a request only', expires_at: '2026-10-23T11:00:00Z', result: { ok: true, summary: '{"delegation":{"status":"queued"}}' } },
  ],
  proposals: [
    { proposal_id: 'prop_1', assistant_id: 'asst_1', kind: 'request_mission', title: 'Add health endpoint', status: 'awaiting_approval', execution: 'not_started', requested_by: 'assistant:asst_1', decided_by: '', result_ref: '', execution_error: '' },
    { proposal_id: 'prop_2', assistant_id: 'asst_1', kind: 'propose_branch', title: 'Fix flaky test', status: 'approved', execution: 'not_started', requested_by: 'assistant:asst_1', decided_by: 'key:boss', result_ref: '', execution_error: '' },
    { proposal_id: 'prop_3', assistant_id: 'asst_1', kind: 'request_mission', title: 'Old mission', status: 'approved', execution: 'executed', requested_by: 'assistant:asst_1', decided_by: 'key:boss', result_ref: 'dlg_9', execution_error: '' },
    // hostile text must be shown as inert text
    { proposal_id: 'prop_4', assistant_id: 'asst_1', kind: 'request_mission', title: '<img src=x onerror="window.pwned=1">', status: 'awaiting_approval', execution: 'not_started', requested_by: 'key:alice', decided_by: '', result_ref: '', execution_error: '' },
  ],
  notifications: [
    { notification_id: 'ntf_1', key: 'ci-fail:1', priority: 'urgent', text: 'CI failed on main', link: '#/ci', state: 'ready', channel: 'dashboard', external: false, created_at: created },
    { notification_id: 'ntf_2', key: 'proposal-executed:3', priority: 'normal', text: 'Handed off: Old mission', link: '', state: 'acknowledged', channel: 'dashboard', external: false, created_at: created },
    { notification_id: 'ntf_3', key: 'approval-waiting:1', priority: 'normal', text: 'An approval is waiting for a second person', link: '#/personal-assistant', state: 'ready', channel: 'dashboard', external: false, created_at: created },
  ],
});

// Gmail and Calendar connected, GitHub status, the Google client saved: the Connectors tab with everything on it.
const APPS = { ready: true, redirect_uri: 'https://cp.example/api/v1/assistant-apps/google/callback', blocked_calendars: ['private@example.com'], providers: [
  { id: 'gmail', name: 'Gmail', kind: 'oauth', provider: 'google', connected: true, account: 'ann@example.com', status: 'connected', scopes: [], client_configured: true },
  { id: 'calendar', name: 'Google Calendar', kind: 'oauth', provider: 'google', connected: true, account: 'ann@example.com', status: 'connected', scopes: [], client_configured: true },
  { id: 'github', name: 'GitHub', kind: 'status', provider: 'github', connected: true, account: 'berlinguyinca', status: 'connected', scopes: [], client_configured: false }] };

/** The /api/v1 answers. Reads only: a surface never mutates anything. Unknown routes answer {} like the live-view tests do. */
export function apiAnswer(pathname, method = 'GET') {
  const db = assistantDb();
  switch (pathname) {
    case '/api/v1/health': return { status: 'ok', capabilities: ['assistant_v1'] };
    case '/api/v1/mission-activity': return { activities: MISSION_ACTIVITY };
    case '/api/v1/missions': return { missions: MISSIONS };
    case '/api/v1/mission-outcomes': return { outcomes: MISSION_OUTCOMES };
    case '/api/v1/assistants': return { assistants: db.assistants };
    case '/api/v1/assistant-approvals': return { approvals: db.approvals };
    case '/api/v1/assistant-connectors': return { connectors: db.connectors };
    case '/api/v1/assistant-proposals': return { proposals: db.proposals };
    case '/api/v1/assistant-notifications': return { notifications: db.notifications };
    case '/api/v1/assistant-apps': return APPS;
    case '/api/v1/assistant-apps/google/calendars': return { calendars: [
      { id: 'ann@example.com', summary: 'Ann Example', primary: true, blocked: false }, { id: 'team@example.com', summary: 'Team', primary: false, blocked: false },
      { id: 'private@example.com', summary: 'Private', primary: false, blocked: true }] };
    case '/api/v1/assistant-apps/google/drafts': return { drafts: [
      { id: 'r-1', to: 'dan@example.com', subject: 'Lunch on Friday', snippet: 'Are you free at noon? I booked the corner table.', gmail_url: 'https://mail.google.com/mail/u/0/#drafts/m1' },
      { id: 'r-2', to: 'team@example.com', subject: 'Notes from the review', snippet: 'Three follow-ups from today.', gmail_url: 'https://mail.google.com/mail/u/0/#drafts/m2' }] };
    case '/api/v1/assistant-reminders': return { timezone: 'America/Los_Angeles', reminders: [
      { id: 'rem_1', text: 'Call the dentist', fire_at: '2026-10-31T09:00:00-07:00', state: 'pending' }] };
    default:
      // the view opens a thread when the assistant has none: answer that one write with a fixed thread
      if (method === 'POST' && /\/threads$/.test(pathname)) return { thread_id: 'thr_1', title: 'New thread', created_at: created };
      return /\/threads$/.test(pathname) ? { threads: [] } : {};
  }
}

/**
 * Open the dashboard at `hash` in a fresh context. The caller (capture) fixes the clock and injects the bundled fonts; this sets up
 * what only this app needs: the stubbed API, a silent event stream, and no calm/theme state from storage.
 * Returns { ctx, page } like openReal.
 */
export async function openDashboard(browser, hash, ctxOpts = {}) {
  const origin = await dashboardOrigin();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, ...ctxOpts });
  const page = await ctx.newPage();
  // no network beyond the loopback static server: any other request is refused
  await ctx.route('**/*', (route) => (route.request().url().startsWith(origin) || route.request().url().startsWith('data:') ? route.fallback() : route.abort()));
  await page.route('**/api/v1/**', (route) => route.fulfill({ json: apiAnswer(new URL(route.request().url()).pathname, route.request().method()) }));
  await page.addInitScript(() => {
    window.EventSource = class { constructor(url) { this.url = String(url); } close() {} };
  });
  await page.clock.setFixedTime(new Date(CFG.screenshots.fixed_clock));
  await page.goto(`${origin}/index.html#${hash}`);
  // Form controls the dashboard leaves unstyled take the browser's own default font (here fontconfig's Liberation Mono), which is a host
  // font and not a bundled one. The harness gives them the design system's sans so the pixels do not depend on the host.
  await page.addStyleTag({ content: 'input,textarea,select,button{font-family:var(--font-sans)}' });
  return { ctx, page };
}

// Size cap (6144 KB for all baselines): the pages are tall (Missions 961px, assistant tabs 1600-2400px) and 100 baselines already take 4.5 MB,
// so the matrix is desktop light, desktop dark and mobile light. Left out: tablet (768px uses the same collapsed layout as mobile, 375px, so
// the mobile baseline covers the layout) and mobile dark (the colours are pinned by desktop dark). Listed in the test matrix as a known limit.
const SKIP = ['tablet-light', 'tablet-dark', 'mobile-dark'];
export const DASHBOARD_SURFACES = [
  { slug: 'dashboard-missions', app: 'dashboard', fullPage: true, skip: SKIP, hash: '/missions', proof: '[data-mission-key]',
    steps: async (page) => { await page.locator('[data-mission-key="pi:M-203"]').waitFor(); await page.locator('#mc-result').filter({ hasText: 'matching current missions' }).waitFor(); await page.evaluate(() => document.activeElement?.blur()); },
    regions: [{ name: 'groups', selector: '#mc-groups' }] },
  { slug: 'dashboard-assistant-proposals', app: 'dashboard', fullPage: true, skip: SKIP, hash: '/personal-assistant', proof: '[data-config]', steps: assistantTab('Proposals', 'Waiting for approval'),
    regions: [{ name: 'panel', selector: '#pa-proposals' }] },
  { slug: 'dashboard-assistant-notifications', app: 'dashboard', fullPage: true, skip: SKIP, hash: '/personal-assistant', proof: '[data-config]', steps: assistantTab('Notifications', 'CI failed on main'),
    regions: [{ name: 'panel', selector: '#pa-notifications' }] },
  { slug: 'dashboard-assistant-approvals', app: 'dashboard', fullPage: true, skip: SKIP, hash: '/personal-assistant', proof: '[data-config]', steps: assistantTab('Approvals', 'Approving allows'),
    regions: [{ name: 'panel', selector: '#pa-approvals' }] },
  { slug: 'dashboard-assistant-connectors', app: 'dashboard', fullPage: true, skip: [...SKIP, 'mobile-light'],   // no mobile baseline: the 6144 KB cap; phone reflow is gated by ui-gates-dashboard-a11y and assistant-connectors.test.mjs
     hash: '/personal-assistant', proof: '[data-config]', steps: connectorsTab },
];

async function connectorsTab(page) {
  await assistantTab('Connectors', 'Apps your assistant can use')(page);
  for (const sel of ['[data-app="gmail"]', '[data-blocked-form]', '[data-draft="r-1"]', '[data-reminder="rem_1"]', '[data-cstatus]']) await page.locator(sel).first().waitFor({ state: 'attached' });
  await page.waitForTimeout(100);
}

function assistantTab(name, text) {
  return async (page) => {
    await page.locator('[data-config]').click();
    await page.getByRole('button', { name, exact: true }).click();
    await page.locator('#pa-panel').getByText(text).first().waitFor();
    // The voice buttons are colour emoji (U+1F3A4, U+1F50A). No bundled font has them, so each host would paint them with its own
    // emoji font (or a box): the one thing in these pages that is not reproducible. They are swapped for ASCII stand-ins here, and
    // kept swapped across redraws; docs/specs/.../test-matrix.md lists this as a known limit of the baselines.
    await page.evaluate(() => {
      const swap = () => { for (const [sel, label] of [['[data-voice]', 'Mic'], ['[data-speak]', 'Say']]) for (const b of document.querySelectorAll(sel)) if (b.textContent !== label) b.textContent = label; };
      swap(); new MutationObserver(swap).observe(document.body, { subtree: true, childList: true, characterData: true });
      document.activeElement?.blur();
    });
  };
}
