// Attention inbox (#/attention): the server derives the items, so an acknowledge or snooze in one browser
// shows in another, the nav badge and the Overview follow the same list, and a failing endpoint is said aloud.
import { test } from 'node:test';
import assert from 'node:assert';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { chromium } from 'playwright';
import { withPage } from './helpers/fixture.mjs';
import { until } from './helpers/live.mjs';

const REPO = path.resolve(import.meta.dirname, '..', '..');
const PY = process.env.PYTHON
  || (fs.existsSync(path.join(REPO, '.venv', 'bin', 'python'))
    ? path.join(REPO, '.venv', 'bin', 'python') : 'python3');

async function startServer(env = {}) {
  const proc = spawn(PY, [path.join(REPO, 'tests', 'playwright', 'server.py')], {
    env: { ...process.env, ...env }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  const port = await new Promise((resolve, reject) => {
    let buf = '';
    const timer = setTimeout(() => reject(new Error('server start timeout')), 20000);
    proc.stdout.on('data', (d) => {
      buf += d.toString();
      const m = buf.match(/PORT=(\d+)/);
      if (m) { clearTimeout(timer); resolve(Number(m[1])); }
    });
    proc.on('exit', (c) => reject(new Error(`server exited early (${c})`)));
    proc.stderr.on('data', (d) => process.stderr.write(`[srv] ${d}`));
  });
  return { proc, port };
}

async function withApp(fn, env = {}) {
  const srv = await startServer({ BURST: '0', INSPECTOR_SEED: '0', ATTENTION_SEED: '1', CI_OFFLINE: '1', ...env });
  let browser;
  try {
    const args = ['--no-sandbox'];
    browser = await chromium.launch(process.env.PLAYWRIGHT_CHROMIUM
      ? { headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM, args } : { headless: true, args });
    return await fn({ browser, base: `http://127.0.0.1:${srv.port}` });
  } finally {
    if (browser) { try { await browser.close(); } catch { /* noop */ } }
    try { srv.proc.kill(); } catch { /* noop */ }
  }
}

const badge = (page) => page.locator('herdr-app-shell .hd-shell__link[data-path="/attention"] .hd-shell__badge');

test('cancelled attention polls cannot overwrite the resumed page or release its request slot', async () => withPage(async (page, base) => {
  await page.goto(`${base}/__blank`);
  await page.evaluate(async () => {
    const { mountAttention } = await import('/attention-view.js');
    window.pending = []; window.hiddenForTest = false;
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => window.hiddenForTest });
    document.body.innerHTML = '<main></main>';
    window.inbox = mountAttention(document.querySelector('main'), { attention: (_, { signal }) => new Promise((resolve, reject) => window.pending.push({ signal, resolve, reject })) });
    window.visibility = hidden => { window.hiddenForTest = hidden; document.dispatchEvent(new Event('visibilitychange')); };
    window.answer = title => ({ items: [{ key: `session:${title}`, title, kind: 'agent_failed', severity: 'critical', state: 'open', fingerprint: 'abc', href: '#/herd' }], counts: { open: 1 } });
    window.visibility(true); window.visibility(false);
    window.pending[0].reject(new DOMException('cancelled', 'AbortError'));
  });
  await page.evaluate(() => window.inbox.refresh());
  assert.equal(await page.evaluate(() => window.pending.length), 2, 'an old cancellation must not unlock the current poll');
  await page.evaluate(() => window.pending[1].resolve(window.answer('Current')));
  await page.getByText('Current', { exact: true }).waitFor();
  await page.evaluate(() => {
    window.inbox.refresh();
    window.visibility(true); window.visibility(false);
    window.pending[3].resolve(window.answer('Newest'));
  });
  await page.getByText('Newest', { exact: true }).waitFor();
  await page.evaluate(() => window.pending[2].resolve(window.answer('Obsolete')));
  assert.equal(await page.getByText('Newest', { exact: true }).count(), 1);
  assert.equal(await page.getByText('Obsolete', { exact: true }).count(), 0);
  await page.evaluate(() => window.inbox.dispose());
}));

test('the page lists server-derived items, an acknowledge keeps them listed, and the badge follows', async () => {
  await withApp(async ({ browser, base }) => {
    const page = await (await browser.newContext()).newPage();
    await page.goto(`${base}/#/attention`);
    const row = page.locator('[data-attention-row^="session:"]');
    await row.waitFor({ timeout: 8000 });
    assert.match(await row.textContent(), /Broken parser agent|Agent failed|pi/);
    assert.ok(await page.locator('[data-attention-row="fleet:drift"]').count() === 1);
    await badge(page).waitFor({ timeout: 8000 });
    assert.equal(await badge(page).getAttribute('data-tone'), 'critical');
    const before = Number(await badge(page).textContent());
    await row.locator('[data-ack]').click();
    await page.locator('details.at-group summary', { hasText: 'Acknowledged (1)' }).waitFor({ timeout: 8000 });
    assert.equal(await page.locator('[data-attention-row^="session:"]').count(), 1);     // still listed, now under Acknowledged
    await page.waitForFunction((n) => Number(document.querySelector('herdr-app-shell').shadowRoot
      .querySelector('.hd-shell__link[data-path="/attention"] .hd-shell__badge')?.textContent) === n - 1, before, { timeout: 15000 });
  });
});

test('an acknowledge in one browser shows in a second browser context', async () => {
  await withApp(async ({ browser, base }) => {
    const a = await (await browser.newContext()).newPage();
    const b = await (await browser.newContext()).newPage();
    await a.goto(`${base}/#/attention`);
    await b.goto(`${base}/#/attention`);
    await a.locator('[data-attention-row^="session:"]').waitFor({ timeout: 8000 });
    await b.locator('[data-attention-row^="session:"] [data-ack]').waitFor({ timeout: 8000 });
    await a.locator('[data-attention-row^="session:"] [data-ack]').click();
    // The other browser has never been told: it learns from the server on its next poll.
    await b.locator('details.at-group summary', { hasText: 'Acknowledged (1)' }).waitFor({ timeout: 30000 });
    assert.equal(await b.locator('[data-attention-row^="session:"] [data-ack]').count(), 0);
    // Reopen from the second browser; the first sees the item open again.
    await b.locator('details.at-group summary', { hasText: 'Acknowledged (1)' }).click();
    await b.locator('[data-attention-row^="session:"] [data-clear]').click();
    await a.locator('[data-attention-row^="session:"] [data-ack]').waitFor({ timeout: 30000 });
  });
});

test('snooze hides an item from the open list and lists it as snoozed', async () => {
  await withApp(async ({ browser, base }) => {
    const page = await (await browser.newContext()).newPage();
    await page.goto(`${base}/#/attention`);
    const drift = page.locator('[data-attention-row="fleet:drift"]');
    await drift.waitFor({ timeout: 8000 });
    await drift.locator('select[data-snooze]').selectOption('3600');
    await page.locator('details.at-group summary', { hasText: 'Snoozed (1)' }).waitFor({ timeout: 8000 });
    assert.equal(await page.locator('ul[aria-label="Open attention items"] [data-attention-row="fleet:drift"]').count(), 0);
  });
});

test('the Overview agrees with the Attention page and says so when the inbox cannot be loaded', async () => {
  await withApp(async ({ browser, base }) => {
    const page = await (await browser.newContext()).newPage();
    // Lazy CI startup briefly reports syncing and adds source:ci to the inbox.
    // Compare the two pages only after the offline fixture reaches its stable state.
    await until(async () => {
      const snapshot = await (await page.request.get(`${base}/api/v1/ci`)).json();
      return snapshot.status === 'not_configured' && snapshot.refreshing === false;
    }, 'offline CI startup to settle', 8000);
    await page.goto(`${base}/#/attention`);
    await page.locator('[data-attention-row^="session:"]').waitFor({ timeout: 8000 });
    const keys = (await page.locator('[data-attention-row]').evaluateAll((els) => els.map((e) => e.dataset.attentionRow))).sort();
    await page.goto(`${base}/#/overview`);
    await page.locator('.ov-attention [data-attention-key^="session:"]').waitFor({ timeout: 8000 });
    const shown = (await page.locator('.ov-attention [data-attention-key]').evaluateAll((els) => els.map((e) => e.dataset.attentionKey))).sort();
    assert.deepEqual(shown, keys);
    assert.equal(await page.locator('.ov-attention a[href="#/attention"]').count(), 1);
    const down = await (await browser.newContext()).newPage();
    await down.route('**/api/v1/attention*', (route) => route.fulfill({ status: 500, body: '{"error":"boom"}', contentType: 'application/json' }));
    await down.goto(`${base}/#/overview`);
    await down.getByText('computed in this browser').waitFor({ timeout: 10000 });
    await down.locator('.ov-attention [data-attention-key^="session:"]').waitFor({ timeout: 8000 });   // the projection still lists the failed agent
  });
});

test('a failing inbox is an explicit error, not an empty list', async () => {
  await withApp(async ({ browser, base }) => {
    const page = await (await browser.newContext()).newPage();
    await page.route('**/api/v1/attention*', (route) => route.fulfill({ status: 500, body: '{"error":"boom"}', contentType: 'application/json' }));
    await page.goto(`${base}/#/attention`);
    await page.getByText('Could not load the attention inbox').waitFor({ timeout: 8000 });
    assert.equal(await page.getByText('No attention items').count(), 0);
  });
});

test('the page fits a 390px screen without horizontal scrolling', async () => {
  await withApp(async ({ browser, base }) => {
    const page = await (await browser.newContext({ viewport: { width: 390, height: 800 } })).newPage();
    await page.goto(`${base}/#/attention`);
    await page.locator('[data-attention-row^="session:"]').waitFor({ timeout: 8000 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok(overflow <= 0, `horizontal overflow ${overflow}px`);
  });
});

// ------------------------------------------------------------- notifications
const stubNotification = () => {
  window.__shown = [];
  class Stub {
    static permission = 'granted';
    static async requestPermission() { return 'granted'; }
    constructor(title, options) { this.title = title; this.options = options; window.__shown.push({ title, options }); }
    close() {}
  }
  window.Notification = Stub;
};

async function withTrigger(fn) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'attention-trigger-'));
  const trigger = path.join(dir, 'go');
  try { await withApp((ctx) => fn({ ...ctx, fire: () => fs.writeFileSync(trigger, '1') }), { ATTENTION_TRIGGER: trigger }); }
  finally { fs.rmSync(dir, { recursive: true, force: true }); }
}

test('a new critical item raises one browser notification with a title and link only', async () => {
  await withTrigger(async ({ browser, base, fire }) => {
    const context = await browser.newContext();
    await context.addInitScript(stubNotification);
    const page = await context.newPage();
    await page.goto(`${base}/#/attention`);
    await page.locator('[data-attention-row^="session:"]').first().waitFor({ timeout: 8000 });
    await page.locator('[data-notify-toggle="on"]').click();
    await page.locator('[data-notify-status="on"]').waitFor();
    await page.waitForResponse((r) => r.url().includes('/api/v1/attention'), { timeout: 10000 });   // the baseline poll
    await page.waitForTimeout(500);
    assert.equal(await page.evaluate(() => window.__shown.length), 0);       // the failure already open is the baseline
    fire();
    await page.waitForFunction(() => window.__shown.length === 1, null, { timeout: 20000 });
    const shown = await page.evaluate(() => window.__shown);
    assert.equal(shown[0].options.body, '');
    assert.deepEqual(shown[0].options.data, { href: '#/attention' });
    const rows = await page.locator('[data-attention-row^="session:"] strong').allTextContents();
    assert.equal(rows.length, 2);
    assert.ok(rows.includes(shown[0].title), `${shown[0].title} not among ${rows}`);
    await page.waitForTimeout(11000);                                         // two more polls: no repeat for the same item
    assert.equal(await page.evaluate(() => window.__shown.length), 1);
  });
});

test('with notifications left off nothing is announced and the page offers to enable them', async () => {
  await withTrigger(async ({ browser, base, fire }) => {
    const context = await browser.newContext();
    await context.addInitScript(stubNotification);
    const page = await context.newPage();
    await page.goto(`${base}/#/attention`);
    await page.locator('[data-notify-status="off"]').waitFor({ timeout: 8000 });
    fire();
    await page.locator('[data-attention-row^="session:"]').nth(1).waitFor({ timeout: 20000 });
    await page.waitForTimeout(1500);
    assert.equal(await page.evaluate(() => window.__shown.length), 0);
  });
});
