// Keyboard use of the Personal Assistant view in a real browser: shortcut sheet, command palette, tab roving, composer, and
// the rule that keys only move focus (nothing is approved or sent by a bare key). Mounts the real view against an in-page
// fake of the /api/v1 routes and records every call. Run: npm run test:ui
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
      res.setHeader('Content-Type', TYPES[extname(path)] || 'application/octet-stream');
      res.end(await readFile(new URL(`../../packages${path}`, import.meta.url)));
    } catch { res.writeHead(404); res.end(); }
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  let browser;
  try {
    const fallback = '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
    browser = await chromium.launch({ headless: true, args: ['--no-sandbox'], executablePath: existsSync(fallback) ? fallback : undefined });
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.evaluate(FAKE_BACKEND);
    await fn(page);
  } finally { await browser?.close(); await new Promise((r) => server.close(r)); }
}

const FAKE_BACKEND = async () => {
  const { HerdrClient } = await import('/herdr-web-client/client.js');
  const { defineComponents } = await import('/herdr-web-components/index.js');
  const { mountPersonalAssistant } = await import('/personal-assistant-view.js');
  defineComponents();
  const now = '2026-10-29T10:00:00Z';
  const mk = (id, name) => ({ assistant_id: id, name, persona: 'helpful', status: 'ready', instructions: '', model_policy: 'auto', enabled: true, created_at: now, updated_at: now });
  const db = {
    assistants: [mk('asst_1', 'Mail assistant'), mk('asst_2', 'CI assistant')],
    approvals: [{ approval_id: 'appr_1', assistant_id: 'asst_1', decision: 'pending', policy_snapshot: 'send_email', tool_id: 'send_email', requested_by: 'assistant:asst_1', expires_at: '2026-10-29T11:00:00Z' }],
    threads: [{ thread_id: 'thr_1', title: 'Inbox watch' }, { thread_id: 'thr_2', title: 'Planning' }],
  };
  const calls = [];
  const reply = (status, json) => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(json) });
  const fetchImpl = async (url, opts = {}) => {
    const method = opts.method || 'GET', path = url.split('?')[0];
    calls.push({ method, path, body: opts.body ? JSON.parse(opts.body) : {} });
    let m;
    if (method === 'GET' && path === '/api/v1/assistants') return reply(200, { assistants: db.assistants });
    if (method === 'GET' && path === '/api/v1/assistant-approvals') return reply(200, { approvals: db.approvals });
    if (method === 'GET' && /\/threads$/.test(path)) return reply(200, { threads: db.threads });
    if (method === 'GET' && (m = /\/threads\/([^/]+)$/.exec(path))) return reply(200, { thread: { thread_id: m[1] }, messages: [{ role: 'assistant', content: `hello from ${m[1]}`, provenance: 'direct' }] });
    if (method === 'POST' && /\/messages$/.test(path)) return reply(202, { run: { run_id: 'run_1', state: 'completed' } });
    if (method === 'POST' && /\/decision$/.test(path)) return reply(200, { decision: 'approved' });
    return reply(404, { error: 'not found' });
  };
  window.__calls = calls;
  mountPersonalAssistant(document.getElementById('stage'), new HerdrClient('', { fetchImpl }));
  await new Promise((r) => setTimeout(r, 120));
};

const mutations = (page) => page.evaluate(() => window.__calls.filter((c) => c.method !== 'GET'));

test('? opens the shortcut sheet, traps focus, and Escape closes it back to where focus was', () => withPage(async (page) => {
  await page.locator('[data-config]').focus();
  await page.keyboard.press('?');
  const dialog = page.getByRole('dialog', { name: 'Keyboard shortcuts' });
  await dialog.waitFor();
  assert.ok(await dialog.getByText('Command palette').isVisible());
  await page.keyboard.press('Tab'); await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => !!document.activeElement.closest('[role=dialog]')), true);   // focus never leaves the dialog
  await page.keyboard.press('Escape');
  assert.equal(await dialog.count(), 0);
  assert.equal(await page.evaluate(() => document.activeElement.dataset.config !== undefined), true);
}));

test('digits and arrows move between Configure tabs, which are a real tablist', () => withPage(async (page) => {
  await page.keyboard.press('2');
  assert.equal(await page.locator('[role=tab][aria-selected=true]').innerText(), 'Permissions');
  assert.equal(await page.evaluate(() => document.activeElement.dataset.tab), 'Permissions');
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('[role=tab][aria-selected=true]').innerText(), 'Memory');
  assert.equal(await page.evaluate(() => document.activeElement.dataset.tab), 'Memory');
  await page.keyboard.press('End');
  assert.equal(await page.locator('[role=tab][aria-selected=true]').innerText(), 'Diagnostics');
  await page.keyboard.press('ArrowRight');                                   // wraps
  assert.equal(await page.locator('[role=tab][aria-selected=true]').innerText(), 'General');
  assert.equal(await page.locator('[role=tab][tabindex="0"]').count(), 1);   // roving tabindex: one tab stop
}));

test('Ctrl+K filters commands by words and Enter runs the first match', () => withPage(async (page) => {
  await page.keyboard.press('Control+k');
  const palette = page.getByRole('dialog', { name: 'Command palette' });
  await palette.waitFor();
  await page.keyboard.type('config diag');
  assert.equal(await palette.getByRole('option').count(), 1);
  await page.keyboard.press('Enter');
  assert.equal(await palette.count(), 0);
  assert.equal(await page.locator('[role=tab][aria-selected=true]').innerText(), 'Diagnostics');
  await page.keyboard.press('Control+k'); await page.keyboard.type('switch ci'); await page.keyboard.press('Enter');
  assert.equal(await page.locator('[data-select]').inputValue(), 'asst_2');
}));

test('/ focuses the message box, Enter sends, bare keys do nothing while typing, Escape leaves the box', () => withPage(async (page) => {
  await page.keyboard.press('/');
  assert.equal(await page.evaluate(() => document.activeElement.dataset.msg !== undefined), true);
  await page.keyboard.type('nnn 2 ? / hello');                               // every one of these is plain text here
  assert.equal(await page.locator('[data-msg]').inputValue(), 'nnn 2 ? / hello');
  assert.equal(await page.getByRole('dialog').count(), 0);
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => window.__calls.some((c) => c.method === 'POST' && c.path.endsWith('/messages')));
  assert.equal((await mutations(page)).find((c) => c.path.endsWith('/messages')).body.content, 'nnn 2 ? / hello');
  assert.equal(await page.locator('[data-msg]').inputValue(), '');                                // cleared after sending
  assert.equal(await page.evaluate(() => document.activeElement.dataset.msg !== undefined), true); // and still focused: keep typing
  await page.keyboard.press('/'); await page.keyboard.press('Escape');
  assert.equal(await page.evaluate(() => document.activeElement.dataset.msg), undefined);
}));

test('a focuses the Approve button but never approves; Enter on the button does', () => withPage(async (page) => {
  await page.keyboard.press('a');
  assert.equal(await page.evaluate(() => document.activeElement.dataset.stripApprove), 'appr_1');
  assert.equal((await mutations(page)).filter((c) => c.path.endsWith('/decision')).length, 0);
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => window.__calls.some((c) => c.path.endsWith('/decision')));
  assert.deepEqual((await mutations(page)).filter((c) => c.path.endsWith('/decision')).map((c) => c.body.decision), ['approved']);
}));

test('[ and ] switch assistant, j and k step through threads, n starts a new one', () => withPage(async (page) => {
  await page.keyboard.press(']');
  assert.equal(await page.locator('[data-select]').inputValue(), 'asst_2');
  await page.keyboard.press('[');
  assert.equal(await page.locator('[data-select]').inputValue(), 'asst_1');
  await page.locator('#pa-threads [data-t]').first().waitFor();
  await page.keyboard.press('j');
  assert.equal(await page.evaluate(() => document.activeElement.textContent), 'Planning');
  await page.getByText('hello from thr_2').waitFor();
  await page.keyboard.press('k');
  await page.getByText('hello from thr_1').waitFor();
  await page.keyboard.press('n');
  await page.waitForFunction(() => window.__calls.some((c) => c.method === 'POST' && c.path.endsWith('/threads')));
}));

test('typing in the assistant picker does not trigger shortcuts', () => withPage(async (page) => {
  await page.locator('[data-select]').focus();
  await page.keyboard.press('n'); await page.keyboard.press('?');
  assert.equal(await page.getByRole('dialog').count(), 0);
  assert.equal((await mutations(page)).length, 0);
}));
