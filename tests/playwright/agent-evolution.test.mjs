// Agent page tabs and the Evolution tab against the real fixture server and real timers (no fake clock): Overview stays the default and
// loads nothing extra, Evolution is read the first time it opens, deep-links, works from the keyboard, shows the phases with their
// rules and honest unknowns, offers a retry on failure, and fits a phone.
import { test } from 'node:test';
import assert from 'node:assert';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

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

async function withApp(fn, viewport = { width: 1280, height: 900 }) {
  const srv = await startServer({ BURST: '0', RELATED_SEED: '1', PR_TRAIL_SEED: '1', AGENT_EVOLUTION_SEED: '1', CI_OFFLINE: '1' });
  let browser;
  try {
    const args = ['--no-sandbox'];
    browser = await chromium.launch(process.env.PLAYWRIGHT_CHROMIUM
      ? { headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM, args } : { headless: true, args });
    const context = await browser.newContext({ viewport });
    const page = await context.newPage();
    const base = `http://127.0.0.1:${srv.port}`;
    const sessions = await (await page.request.get(`${base}/api/v1/sessions`)).json();
    const agent = (sessions.sessions || []).find((s) => s.repository_id === 'org/app' && s.branch === 'fix/parser');
    assert.ok(agent, 'the seeded dashboard agent exists');
    return await fn({ page, base, id: agent.session_id, shot: process.env.EVOLUTION_SHOT });
  } finally {
    if (browser) { try { await browser.close(); } catch { /* noop */ } }
    try { srv.proc.kill(); } catch { /* noop */ }
  }
}

const open = (page, base, id, query = '') => page.goto(`${base}/#/herd/session/${encodeURIComponent(id)}${query}`);

test('Overview is the default tab and the Evolution record is not requested until its tab opens', async () => {
  await withApp(async ({ page, base, id }) => {
    const requests = [];
    page.on('request', (r) => { if (/\/evolution$/.test(new URL(r.url()).pathname)) requests.push(r.url()); });
    await open(page, base, id);
    await page.locator('[data-agent-tabs]').waitFor({ timeout: 10000 });
    assert.equal(await page.locator('#agent-tab-overview').getAttribute('aria-selected'), 'true');
    assert.equal(await page.locator('#agent-panel-evolution').isHidden(), true);
    assert.equal(await page.locator('#agent-panel-overview').isVisible(), true);
    assert.ok(await page.locator('#session-details').count() === 1 && await page.locator('#session-story').count() === 1, 'the overview content is all still there');
    await page.waitForTimeout(1500);
    assert.deepEqual(requests, [], 'nothing is read for the hidden tab');
    await page.locator('#agent-tab-evolution').click();
    await page.locator('[data-phase="assigned"]').waitFor({ timeout: 10000 });
    assert.equal(requests.length >= 1, true);
    assert.match(page.url(), /\?tab=evolution$/);
    assert.equal(await page.locator('#agent-panel-overview').isHidden(), true);
  });
});

test('Evolution shows the phases in order, each item with its rule, and unknown figures with their reasons', async () => {
  await withApp(async ({ page, base, id, shot }) => {
    await open(page, base, id, '?tab=evolution');
    await page.locator('[data-phase="assigned"]').waitFor({ timeout: 10000 });
    assert.equal(await page.locator('#view herdr-error').count(), 0);
    const order = await page.locator('[data-phase]').evaluateAll((els) => els.map((e) => e.getAttribute('data-phase')));
    assert.deepEqual(order, ['assigned', 'worked', 'handed_off', 'produced', 'verified', 'outcome', 'cost']);
    const text = await page.locator('#agent-panel-evolution').innerText();
    assert.match(text, /fix the parser crash on empty input/);
    assert.match(text, /reproduced the crash/);
    assert.match(text, /Handed to this agent/);
    assert.match(text, /write the parser tests/, 'handoffs out are listed');
    assert.match(text, /port it to Rust/);
    assert.match(text, /\(restart, derived\)/);
    assert.match(text, /via its session id/);
    assert.equal(await page.locator('[data-phase="handed_off"] li[data-problem="true"]').count(), 1, 'only the rejected handoff is a problem');
    assert.equal(await page.locator('[aria-label="Learning record"] [data-signal="tool_calls_retried"]').getAttribute('data-known'), 'true');
    assert.equal(await page.locator('[aria-label="Learning record"] [data-signal="tool_calls_retried"] b').innerText(), '1');
    assert.equal(await page.locator('[aria-label="Learning record"] [data-signal="cost_usd_estimate"]').getAttribute('data-known'), 'false');
    assert.match(await page.locator('[aria-label="Learning record"] [data-signal="cost_usd_estimate"]').innerText(), /price table/);
    assert.equal(await page.locator('[data-phase="produced"] a[href^="#/pr/org/app/214"]').count(), 1);
    assert.match(await page.locator('[data-ev-sources]').innerText(), /ci: unavailable/);
    const times = await page.locator('.ev-items time').evaluateAll((els) => els.map((e) => e.textContent).filter(Boolean));
    assert.ok(times.length >= 5);
    if (shot) await page.screenshot({ path: shot, fullPage: true });
    await page.locator('[data-phase="produced"] a[href^="#/pr/org/app/214"]').click();
    await page.waitForURL(/#\/pr\/org\/app\/214/);
    await page.locator('[data-pt-head]').waitFor({ timeout: 10000 });
  });
});

test('the tabs work from the keyboard and the address follows without a reload', async () => {
  await withApp(async ({ page, base, id }) => {
    await open(page, base, id);
    await page.locator('#agent-tab-overview').waitFor({ timeout: 10000 });
    await page.locator('#agent-tab-overview').focus();
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.locator('#agent-tab-evolution').getAttribute('aria-selected'), 'true');
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'agent-tab-evolution');
    await page.locator('[data-phase="assigned"]').waitFor({ timeout: 10000 });
    await page.keyboard.press('Home');
    assert.equal(await page.locator('#agent-tab-overview').getAttribute('aria-selected'), 'true');
    assert.equal(await page.locator('#agent-panel-overview').isVisible(), true);
    assert.doesNotMatch(page.url(), /tab=evolution/);
    await page.keyboard.press('End');
    assert.match(page.url(), /tab=evolution/);
    const roles = await page.locator('[data-agent-tabs] [role=tab]').evaluateAll((els) => els.map((e) => [e.getAttribute('aria-controls'), e.tabIndex, e.getBoundingClientRect().height >= 44]));
    assert.deepEqual(roles.map((r) => r[2]), [true, true]);
    assert.equal(await page.locator('#agent-panel-evolution').getAttribute('aria-labelledby'), 'agent-tab-evolution');
  });
});

test('a failed read says so, offers Retry, and the retry recovers', async () => {
  await withApp(async ({ page, base, id }) => {
    let fail = true;
    await page.route('**/api/v1/sessions/*/evolution', (route) => (fail ? route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"boom"}' }) : route.continue()));
    await open(page, base, id, '?tab=evolution');
    await page.locator('[data-ev-error]:not([hidden])').waitFor({ timeout: 10000 });
    assert.match(await page.locator('[data-ev-error]').innerText(), /could not be loaded/);
    fail = false;
    await page.locator('[data-ev-retry]').click();
    await page.locator('[data-phase="assigned"]').waitFor({ timeout: 10000 });
    assert.equal(await page.locator('[data-ev-error]').isHidden(), true);
  });
});

test('on a phone the tabs and phases fit with no sideways scroll and 44px targets', async () => {
  await withApp(async ({ page, base, id }) => {
    await open(page, base, id, '?tab=evolution');
    await page.locator('[data-phase="assigned"]').waitFor({ timeout: 10000 });
    const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok(over <= 0, `horizontal overflow ${over}px`);
    const small = await page.locator('#agent-panel-evolution a:visible, #agent-panel-evolution button:visible, [data-agent-tab]').evaluateAll(
      (els) => els.filter((e) => e.getBoundingClientRect().height < 43).map((e) => e.textContent.trim().slice(0, 30)));
    assert.deepEqual(small, []);
  }, { width: 390, height: 844 });
});
