// PR trail page against the real fixture server and real timers (no fake clock): the agent that produced org/app#214, the
// handoffs out of it (one accepted, one rejected), its review, the honest "unknown" signals while CI data is offline, the
// links into the page, an unknown pull request, and phone width.
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
  const srv = await startServer({ BURST: '0', RELATED_SEED: '1', PR_TRAIL_SEED: '1', CI_OFFLINE: '1' });
  let browser;
  try {
    const args = ['--no-sandbox'];
    browser = await chromium.launch(process.env.PLAYWRIGHT_CHROMIUM
      ? { headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM, args } : { headless: true, args });
    const page = await (await browser.newContext({ viewport })).newPage();
    return await fn({ page, base: `http://127.0.0.1:${srv.port}` });
  } finally {
    if (browser) { try { await browser.close(); } catch { /* noop */ } }
    try { srv.proc.kill(); } catch { /* noop */ }
  }
}

test('the trail shows the agent, both handoffs, the review and unknown signals while CI is unreachable', async () => {
  await withApp(async ({ page, base }) => {
    await page.goto(`${base}/#/pr/org/app/214`);
    await page.locator('[data-pt-head]').waitFor({ timeout: 10000 });
    assert.equal(await page.locator('#view herdr-error').count(), 0);
    const text = await page.locator('#pr-trail').innerText();
    assert.match(text, /org\/app #214/);
    assert.match(text, /write the parser tests/);
    assert.match(text, /port it to Rust/);
    assert.match(text, /Code review approved/i);
    assert.match(text, /ci: unavailable/);
    assert.equal(await page.locator('li[data-kind="handoff"][data-problem="true"]').count(), 1, 'only the rejected handoff is a problem');
    assert.equal(await page.locator('[data-signal="ci_green_first_try"]').getAttribute('data-known'), 'false');
    assert.equal(await page.locator('[data-signal="handoff_problems"] b').innerText(), '1');
    const times = await page.locator('.pt-line time').evaluateAll((els) => els.map((e) => e.textContent));
    assert.ok(times.length >= 4);
    await page.locator('.pt-line a[href^="#/herd/session/"]').first().click();
    await page.waitForURL(/#\/herd\/session\//);
  });
});

test('an unknown pull request says so and offers a retry, not an empty page', async () => {
  await withApp(async ({ page, base }) => {
    await page.goto(`${base}/#/pr/org/app/9999`);
    await page.locator('[data-pt-error]:not([hidden])').waitFor({ timeout: 10000 });
    assert.match(await page.locator('[data-pt-error]').innerText(), /No pull request with this number is recorded/);
    assert.equal(await page.locator('[data-pt-retry]:not([hidden])').count(), 1);
  });
});

test('the agent page links its pull request to the trail', async () => {
  await withApp(async ({ page, base }) => {
    const { sessions } = await (await fetch(`${base}/api/v1/sessions`)).json();
    const s = sessions.find((x) => x.repository_id === 'org/app');
    await page.goto(`${base}/#/herd/session/${encodeURIComponent(s.session_id)}`);
    const link = page.locator('a[data-pr-trail]').first();
    await link.waitFor({ timeout: 10000 });
    await link.click();
    await page.waitForURL(/#\/pr\/org\/app\/214/);
    await page.locator('[data-pt-head]').waitFor({ timeout: 10000 });
  });
});

test('at phone width nothing overflows and every trail link is 44px tall', async () => {
  await withApp(async ({ page, base }) => {
    await page.goto(`${base}/#/pr/org/app/214`);
    await page.locator('[data-pt-head]').waitFor({ timeout: 10000 });
    const over = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
    assert.ok(over <= 0, `horizontal overflow ${over}px`);
    const small = await page.locator('.pt-line a').evaluateAll((els) => els.map((e) => e.getBoundingClientRect().height).filter((h) => h < 43.5));
    assert.deepEqual(small, [], 'every link in the trail is at least 44px tall');
  }, { width: 390, height: 844 });
});
