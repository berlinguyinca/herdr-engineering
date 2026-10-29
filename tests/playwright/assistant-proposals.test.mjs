// Assistant follow-ups in a real browser: proposals, approvals with dry-run, notification delivery records, and the
// connector "Test" button. It mounts the real view (packages/personal-assistant-view.js) against an in-page fake of
// the /api/v1 routes, so it needs no control-plane process. The fake mirrors the server's contract (403
// approval_needed, distinct-approver refusal) and records every call so the tests can assert WHICH route was hit.
// Run: npm run test:ui
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { extname } from 'node:path';
import { chromium } from 'playwright';

const TYPES = { '.js': 'application/javascript', '.css': 'text/css', '.html': 'text/html', '.woff2': 'font/woff2', '.svg': 'image/svg+xml' };
const PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/herdr-design-system/theme.css"><link rel="stylesheet" href="/herdr-design-system/base.css">
<link rel="stylesheet" href="/herdr-web-components/components.css"><link rel="stylesheet" href="/personal-assistant.css"></head>
<body><main id="stage"></main></body></html>`;

async function withPage(fn, { width = 1100 } = {}) {
  const server = createServer(async (req, res) => {
    const path = req.url.split('?')[0];
    if (path === '/') { res.setHeader('Content-Type', 'text/html'); res.end(PAGE); return; }
    try {
      const file = new URL(`../../packages${path}`, import.meta.url);
      res.setHeader('Content-Type', TYPES[extname(path)] || 'application/octet-stream');
      res.end(await readFile(file));
    } catch { res.writeHead(404); res.end(); }
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  let browser;
  try {
    const fallback = '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
    const executablePath = existsSync(fallback) ? fallback : undefined;   // else Playwright's own browser
    browser = await chromium.launch({ headless: true, args: ['--no-sandbox'], executablePath });
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.evaluate(FAKE_BACKEND);
    await fn(page);
  } finally { await browser?.close(); await new Promise((r) => server.close(r)); }
}

// Runs in the page: builds the fake API, mounts the real view, exposes window.__calls and window.__db.
const FAKE_BACKEND = async () => {
  const { HerdrClient } = await import('/herdr-web-client/client.js');
  const { defineComponents } = await import('/herdr-web-components/index.js');
  const { mountPersonalAssistant } = await import('/personal-assistant-view.js');
  defineComponents();
  const now = '2026-10-29T10:00:00Z';
  const db = {
    assistants: [{ assistant_id: 'asst_1', name: 'Personal Assistant', persona: 'helpful', status: 'ready', instructions: '', model_policy: 'auto', enabled: true, created_at: now, updated_at: now }],
    connectors: [{ connector_id: 'conn_1', name: 'Weather', kind: 'http', base_url: 'https://w.example.com', auth_present: false, enabled: true }],
    approvals: [{ approval_id: 'appr_1', assistant_id: 'asst_1', decision: 'pending', policy_snapshot: 'request_review:claude', tool_id: 'request_review',
      action_status: 'requested', executable: true, requested_by: 'key:alice', dry_run: 'Ask claude to review pr:o/r#7; records a request only', expires_at: '2026-10-29T11:00:00Z', result: {} }],
    proposals: [
      { proposal_id: 'prop_1', assistant_id: 'asst_1', kind: 'request_mission', title: 'Add health endpoint', status: 'awaiting_approval', execution: 'not_started', requested_by: 'assistant:asst_1', decided_by: '', result_ref: '', execution_error: '' },
      { proposal_id: 'prop_2', assistant_id: 'asst_1', kind: 'propose_branch', title: 'Fix flaky test', status: 'approved', execution: 'not_started', requested_by: 'assistant:asst_1', decided_by: 'key:boss', result_ref: '', execution_error: '' },
      { proposal_id: 'prop_3', assistant_id: 'asst_1', kind: 'request_mission', title: 'Old mission', status: 'approved', execution: 'executed', requested_by: 'assistant:asst_1', decided_by: 'key:boss', result_ref: 'dlg_9', execution_error: '' },
      { proposal_id: 'prop_4', assistant_id: 'asst_1', kind: 'request_mission', title: '<img src=x onerror="window.pwned=1">', status: 'awaiting_approval', execution: 'not_started', requested_by: 'key:alice', decided_by: '', result_ref: '', execution_error: '' },
    ],
    notifications: [
      { notification_id: 'ntf_1', key: 'ci-fail:1', priority: 'urgent', text: 'CI failed on main', link: '#/ci', state: 'ready', channel: 'dashboard', external: false, created_at: now },
      { notification_id: 'ntf_2', key: 'proposal-executed:3', priority: 'normal', text: 'Handed off: Old mission', link: '', state: 'acknowledged', channel: 'dashboard', external: false, created_at: now },
    ],
    connectorMode: 'approval_needed',
  };
  const calls = [];
  const reply = (status, json) => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(json) });
  const fetchImpl = async (url, opts = {}) => {
    const method = opts.method || 'GET';
    const path = url.split('?')[0];
    const body = opts.body ? JSON.parse(opts.body) : {};
    calls.push({ method, path, body });
    let m;
    if (method === 'GET' && path === '/api/v1/assistants') return reply(200, { assistants: db.assistants });
    if (method === 'GET' && path === '/api/v1/assistant-approvals') return reply(200, { approvals: db.approvals });
    if (method === 'GET' && path === '/api/v1/assistant-connectors') return reply(200, { connectors: db.connectors });
    if (method === 'GET' && path === '/api/v1/assistant-proposals') return reply(200, { proposals: db.proposals });
    if (method === 'GET' && path === '/api/v1/assistant-notifications') return reply(200, { notifications: db.notifications });
    if (method === 'GET' && /\/threads$/.test(path)) return reply(200, { threads: [] });
    if (method === 'GET' && (m = /^\/api\/v1\/assistant-proposals\/([^/]+)\/preview$/.exec(path))) return reply(200, { dry_run: true, description: `Queue a mission for pi: ${m[1]}. Writes no code and merges nothing.`, payload_intact: true });
    if (method === 'POST' && (m = /^\/api\/v1\/assistant-proposals\/([^/]+)\/decision$/.exec(path))) {
      const x = db.proposals.find((p) => p.proposal_id === m[1]);
      if (x.requested_by === 'key:alice' && body.decision === 'approved') return reply(403, { error: 'the approver must differ from the requester', code: 'forbidden' });
      x.status = body.decision; x.decided_by = 'key:boss';
      if (body.decision === 'approved') { x.execution = 'executed'; x.result_ref = 'dlg_1'; }
      return reply(200, x);
    }
    if (method === 'POST' && (m = /^\/api\/v1\/assistant-proposals\/([^/]+)\/execute$/.exec(path))) {
      const x = db.proposals.find((p) => p.proposal_id === m[1]); x.execution = 'executed'; x.result_ref = 'dlg_2'; return reply(200, x);
    }
    if (method === 'POST' && (m = /^\/api\/v1\/assistant-approvals\/([^/]+)\/decision$/.exec(path))) {
      const ap = db.approvals.find((a) => a.approval_id === m[1]);
      if (body.decision === 'approved' && ap.requested_by === 'key:me') return reply(403, { error: 'the approver must differ from the requester for this action', code: 'forbidden' });
      ap.decision = body.decision;
      if (body.decision === 'approved') { ap.action_status = 'completed'; ap.result = { ok: true, summary: '{"delegation":{"status":"queued"}}' }; }
      return reply(200, { ...ap, execution: { status: ap.action_status } });
    }
    if (method === 'POST' && (m = /^\/api\/v1\/assistant-notifications\/([^/]+)\/ack$/.exec(path))) {
      const n = db.notifications.find((x) => x.notification_id === m[1]); n.state = 'acknowledged'; return reply(200, n);
    }
    if (method === 'POST' && path === '/api/v1/assistant-connectors/conn_1/test') {
      if (db.connectorMode === 'approval_needed') return reply(403, { error: 'approval required', code: 'approval_needed', approval_id: 'appr_77', dry_run: 'Call connector conn_1 with GET /.' });
      if (db.connectorMode === 'no_grant') return reply(403, { error: 'connector call is not authorized' });
      return reply(200, { ok: true, status: 200 });
    }
    if (path === '/api/v1/assistant-connectors/conn_1/request') return reply(400, { error: 'run_id and idempotency_key are required' });
    return reply(404, { error: 'not found' });
  };
  window.__calls = calls; window.__db = db;
  const client = new HerdrClient('', { fetchImpl });
  mountPersonalAssistant(document.getElementById('stage'), client);
  await new Promise((r) => setTimeout(r, 50));
  document.querySelector('[data-config]').click();
};

const openTab = async (page, name) => {
  await page.getByRole('button', { name, exact: true }).click();
};

test('proposals are grouped pending, approved and executed; preview is a dry run; approve hands off', () => withPage(async (page) => {
  await openTab(page, 'Proposals');
  await page.getByRole('heading', { name: /Waiting for approval/ }).waitFor();
  assert.ok(await page.getByRole('heading', { name: /Approved, not yet run \(1\)/ }).isVisible());
  assert.ok(await page.getByRole('heading', { name: /Executed \(1\)/ }).isVisible());
  assert.equal(await page.evaluate(() => window.pwned), undefined);                 // hostile title is inert text
  assert.equal(await page.locator('#pa-panel img').count(), 0);
  await page.getByRole('button', { name: 'Preview Add health endpoint' }).click();
  await page.getByText(/Dry run: Queue a mission for pi: prop_1/).waitFor();
  assert.equal((await page.evaluate(() => window.__calls)).filter((c) => c.method !== 'GET' && c.path.includes('assistant-proposals')).length, 0);   // preview mutates nothing
  await page.getByRole('button', { name: 'Approve Add health endpoint' }).click();
  await page.getByText('Approved and handed off.').waitFor();
  const decisions = (await page.evaluate(() => window.__calls)).filter((c) => c.path.endsWith('/decision'));
  assert.deepEqual(decisions.map((c) => [c.path, c.body.decision]), [['/api/v1/assistant-proposals/prop_1/decision', 'approved']]);
}));

test('the distinct-approver refusal from the server is shown as an alert and nothing changes', () => withPage(async (page) => {
  await openTab(page, 'Proposals');
  await page.getByRole('button', { name: /^Approve <img/ }).click();
  const alert = page.locator('#pa-panel .pa-flash[role="alert"]');
  await alert.waitFor();
  assert.match(await alert.textContent(), /approver must differ from the requester/);
  assert.equal(await page.evaluate(() => window.__db.proposals.find((p) => p.proposal_id === 'prop_4').status), 'awaiting_approval');
  assert.ok((await page.getByText('The approver must differ from the requester.').count()) >= 1);   // the rule is stated up front
}));

test('an approved proposal that has not run offers Run now, which calls the execute route', () => withPage(async (page) => {
  await openTab(page, 'Proposals');
  await page.getByRole('button', { name: 'Run approved Fix flaky test now' }).click();
  await page.getByText('Execution executed.').waitFor();
  assert.ok((await page.evaluate(() => window.__calls)).some((c) => c.method === 'POST' && c.path === '/api/v1/assistant-proposals/prop_2/execute'));
}));

test('approvals show what approving allows, the requester, and the result after execution', () => withPage(async (page) => {
  await openTab(page, 'Approvals');
  await page.getByText(/Approving allows: Ask claude to review/).waitFor();
  assert.ok(await page.getByText(/Requested by key:alice/).isVisible());
  assert.ok(await page.getByText('The approver must be a different person from the requester.').isVisible());
  await page.getByRole('button', { name: 'Approve request_review:claude' }).click();
  await page.getByText(/Approved; execution completed/).waitFor();
  assert.ok(await page.locator('[data-result]').first().isVisible());
  assert.equal(await page.getByRole('button', { name: /^Approve / }).count(), 0);   // decided: no second decision offered
}));

test('notifications list delivery records, say nothing left the dashboard, and can be acknowledged', () => withPage(async (page) => {
  await openTab(page, 'Notifications');
  await page.getByText('CI failed on main').waitFor();
  assert.equal(await page.getByText('dashboard · not sent externally').count(), 2);
  assert.equal(await page.getByRole('link', { name: 'Open' }).getAttribute('href'), '#/ci');
  await page.getByRole('button', { name: 'Acknowledge ci-fail:1' }).click();
  await page.getByText('Acknowledged.').waitFor();
  assert.equal(await page.getByRole('button', { name: /^Acknowledge/ }).count(), 0);
}));

test('connector Test uses the gated test route and shows the approval-pending state (no more 400)', () => withPage(async (page) => {
  await openTab(page, 'Connectors');
  await page.getByRole('button', { name: 'Test connector Weather' }).click();
  const status = page.locator('[data-cstatus="conn_1"]');
  await status.getByText(/Approval pending \(appr_77\)/).waitFor();
  assert.match(await status.textContent(), /Nothing was called/);
  const calls = await page.evaluate(() => window.__calls);
  assert.ok(calls.some((c) => c.path === '/api/v1/assistant-connectors/conn_1/test' && c.body.assistant_id === 'asst_1'));
  assert.ok(!calls.some((c) => c.path.endsWith('/conn_1/request')));
  await status.getByRole('button', { name: 'Open approvals' }).click();
  await page.getByText(/Approving allows/).waitFor();                              // landed on the Approvals tab
}));

test('connector Test reports a refusal and an ok answer in the row', () => withPage(async (page) => {
  await openTab(page, 'Connectors');
  const status = page.locator('[data-cstatus="conn_1"]');
  await page.evaluate(() => { window.__db.connectorMode = 'no_grant'; });
  await page.getByRole('button', { name: 'Test connector Weather' }).click();
  await status.getByText('Connector test: connector call is not authorized').waitFor();
  await page.evaluate(() => { window.__db.connectorMode = 'ok'; });
  await page.getByRole('button', { name: 'Test connector Weather' }).click();
  await status.getByText('Connector test: ok').waitFor();
}));

test('keyboard: Approve is reachable and activates with Enter; every control has an accessible name', () => withPage(async (page) => {
  await openTab(page, 'Approvals');
  await page.getByText(/Approving allows/).waitFor();
  const nameless = await page.evaluate(() => [...document.querySelectorAll('#pa-panel button, #pa-panel a')]
    .filter((b) => !(b.getAttribute('aria-label') || b.textContent || '').trim()).length);
  assert.equal(nameless, 0);
  await page.getByRole('button', { name: 'Approve request_review:claude' }).focus();
  await page.keyboard.press('Enter');
  await page.getByText(/Approved; execution completed/).waitFor();
  assert.ok(await page.locator('[role="status"][aria-live="polite"]').count() >= 1);
}));

test('mobile: the proposals and notifications tabs do not scroll sideways at 375px', () => withPage(async (page) => {
  for (const tab of ['Proposals', 'Notifications', 'Approvals']) {
    await openTab(page, tab);
    await page.waitForTimeout(100);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok(overflow <= 0, `${tab} overflows by ${overflow}px`);
    const small = await page.evaluate(() => [...document.querySelectorAll('#pa-panel .hd-btn')].filter((b) => b.getBoundingClientRect().height < 32).length);
    assert.equal(small, 0, `${tab} has buttons under 32px tall`);
  }
}, { width: 375 }));
