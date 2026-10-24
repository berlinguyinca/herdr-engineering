// T10 smoke: the virtual tab of an external (remote) agent in a real browser. It mounts the pure renderer
// from packages/collab-tabs.js, so it needs no server: it checks the roles a keyboard/screen-reader user relies
// on, that the Remote marker and the "no terminal" notice are visible, that hostile text from the agent never
// becomes markup or script, and that a revoked agent keeps its tab. Run: npm run test:ui
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { chromium } from 'playwright';

const evil = '<img src=x onerror="window.pwned=1">"\'&';
const row = (id, extra = {}) => ({
  agent_id: id, node_id: `agent:${id}`, kind: 'external', label: 'Dana', state: 'active', invited_by: 'alice', invited_by_email: 'alice@example.org',
  enrolled_at: '2026-10-23T10:00:00+00:00', expires_at: 1800086400, actions: ['read', 'post'], lineage: true, declared_harness: 'claude-code',
  presence: { state: 'online', status: 'working', last_activity: 'reading the spec', last_seen_at: '2026-10-23T10:05:00+00:00', age_seconds: 12 }, ...extra });

async function withPage(fn) {
  const server = createServer(async (req, res) => {
    if (req.url === '/') { res.setHeader('Content-Type', 'text/html'); res.end('<!doctype html><html lang="en"><body><main id="host"></main></body></html>'); return; }
    try { const file = new URL(`../../packages${req.url}`, import.meta.url); res.setHeader('Content-Type', req.url.endsWith('.css') ? 'text/css' : 'application/javascript'); res.end(await readFile(file)); }
    catch { res.writeHead(404); res.end(); }
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  let browser;
  try {
    const fallback = '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
    const executablePath = process.env.PLAYWRIGHT_CHROMIUM || (existsSync(fallback) ? fallback : undefined);
    browser = await chromium.launch({ headless: true, args: ['--no-sandbox'], executablePath });
    const page = await browser.newPage();
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.evaluate(async () => {
      const m = await import('/collab-tabs.js');
      window.draw = (rows, opts) => { document.querySelector('#host').innerHTML = m.renderTabsPanes(m.buildTabsPanes([], null, rows), null, opts); };
    });
    await fn(page);
  } finally { await browser?.close(); await new Promise((r) => server.close(r)); }
}

test('a remote agent is a labelled virtual tab with no terminal, and hostile text stays inert', async () => withPage(async (page) => {
  await page.evaluate(([rows]) => window.draw(rows, { canManage: true }), [[row('agent_a', { label: evil, invited_by_email: evil, presence: { state: 'online', status: 'working', last_activity: evil, last_seen_at: evil, age_seconds: 1 } })]]);
  assert.equal(await page.getByRole('tablist', { name: 'Primary agents' }).count(), 1);
  const tab = page.getByRole('tab').first();
  assert.equal(await tab.getAttribute('aria-selected'), 'true');
  assert.ok((await tab.textContent()).includes('Remote'));
  assert.ok((await page.getByRole('tabpanel').textContent()).includes('no terminal and no shell access'));
  assert.equal(await page.evaluate(() => window.pwned), undefined);
  assert.equal(await page.locator('#host img').count(), 0);
  assert.ok((await page.getByRole('tabpanel').textContent()).includes('<img src=x'));       // shown as text
  assert.equal(await page.locator('button[data-revoke]').count(), 1);
  assert.equal(await page.getByRole('button', { name: /terminal|shell|attach/i }).count(), 0);
}));

test('a revoked remote agent keeps its tab, marked revoked, with no revoke control', async () => withPage(async (page) => {
  await page.evaluate(([rows]) => window.draw(rows, { canManage: true }), [[row('agent_b', { state: 'revoked', revoked_at: '2026-10-23T11:00:00+00:00', revoked_by: 'user:alice' })]]);
  assert.ok((await page.getByRole('tab').first().textContent()).includes('revoked'));
  assert.ok((await page.getByRole('tabpanel').textContent()).includes('what it already did stays visible'));
  assert.equal(await page.locator('button[data-revoke]').count(), 0);
}));

test('stale presence is not shown as a live status', async () => withPage(async (page) => {
  await page.evaluate(([rows]) => window.draw(rows, {}), [[row('agent_c', { presence: { state: 'stale', status: null, last_status: 'working', last_activity: 'x', age_seconds: 500 } })]]);
  const text = await page.getByRole('tabpanel').textContent();
  assert.ok(text.includes('stale'));
  assert.ok(text.includes('not shown as current'));
  assert.ok(!(await page.getByRole('tab').first().textContent()).includes('working'));
}));
