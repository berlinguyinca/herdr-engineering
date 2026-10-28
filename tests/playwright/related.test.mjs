// Cross-linking: the Related panel on detail pages (mission, agent, host, service), click-through
// between them, the failed-request state, and containment at phone width.
import { test } from 'node:test';
import assert from 'node:assert';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const REPO = path.resolve(import.meta.dirname, '..', '..');
const PY = process.env.PYTHON
  || (fs.existsSync(path.join(REPO, '.venv', 'bin', 'python'))
    ? path.join(REPO, '.venv', 'bin', 'python') : 'python3');

async function startServer(env = {}, spawnServer = spawn) {
  const proc = spawnServer(PY, [path.join(REPO, 'tests', 'playwright', 'server.py')], {
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
    proc.once('exit', (c) => { clearTimeout(timer); reject(new Error(`server exited early (${c})`)); });
    proc.once('error', (error) => { clearTimeout(timer); reject(error); });
    proc.stderr.on('data', (d) => process.stderr.write(`[srv] ${d}`));
  }).catch((error) => {
    // Setup can fail before withApp owns the fixture and reaches its finally block.
    proc.kill();
    throw error;
  });
  return { proc, port };
}

for (const scenario of ['timeout', 'early exit', 'spawn error']) {
  test(`startupcleanup: ${scenario} leaves no child or pending deadline`, { timeout: 5000 }, async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const schedule = globalThis.setTimeout;
    let deadline;
    t.mock.method(globalThis, 'setTimeout', (callback, delay) => {
      deadline = t.mock.fn(callback);
      return schedule(deadline, delay);
    });
    let proc;
    let closed;
    const starting = startServer({}, (_command, _args, options) => {
      proc = scenario === 'spawn error'
        ? spawn(`${process.execPath}-missing-fixture`, [], options)
        : spawn(process.execPath, ['-e', scenario === 'early exit'
          ? 'process.exit(23)' : 'setInterval(() => {}, 1000)'], options);
      closed = new Promise((resolve) => proc.once('close', (code, signal) => resolve({ code, signal })));
      // Even a failing assertion must not leak the regression's real child.
      t.after(async () => { proc.kill('SIGKILL'); await closed; });
      return proc;
    });
    const rejected = assert.rejects(starting, scenario === 'timeout'
      ? /server start timeout/ : scenario === 'early exit'
        ? /server exited early \(23\)/ : { code: 'ENOENT' });
    if (scenario === 'timeout') {
      await once(proc, 'spawn');
      t.mock.timers.tick(20000);
    }
    await rejected;
    if (scenario === 'timeout') assert.equal(proc.killed, true, 'startup failure must terminate the child');
    const result = await closed;
    if (scenario !== 'spawn error') {
      assert.deepEqual(result, scenario === 'timeout'
        ? { code: null, signal: 'SIGTERM' } : { code: 23, signal: null });
      assert.throws(() => process.kill(proc.pid, 0), { code: 'ESRCH' });
    }
    // An exit/error must cancel the deadline, not merely settle its promise first.
    t.mock.timers.tick(20000);
    assert.equal(deadline.mock.callCount(), scenario === 'timeout' ? 1 : 0);
  });
}

async function withApp(fn, env = {}) {
  const srv = await startServer({ BURST: '0', RELATED_SEED: '1', CI_OFFLINE: '1', ...env });
  let browser;
  try {
    const args = ['--no-sandbox'];
    browser = await chromium.launch(process.env.PLAYWRIGHT_CHROMIUM
      ? { headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM, args } : { headless: true, args });
    const page = await browser.newPage();
    return await fn({ page, base: `http://127.0.0.1:${srv.port}` });
  } finally {
    if (browser) { try { await browser.close(); } catch { /* noop */ } }
    try { srv.proc.kill(); } catch { /* noop */ }
  }
}

async function openAlpha(page, base) {
  const { missions } = await (await fetch(`${base}/api/v1/missions`)).json();
  const alpha = missions.find((m) => m.title === 'Alpha');
  await page.goto(`${base}/#/missions/${encodeURIComponent(alpha.mission_id)}`);
}

const group = (page, title) => page.locator('.rel dt', { hasText: title }).locator('xpath=following-sibling::dd[1]');

test('a mission links to its agent, host, review, pull request and CI run, and the links arrive', async () => {
  await withApp(async ({ page, base }) => {
    await openAlpha(page, base);
    await page.locator('.rel dl').waitFor({ timeout: 8000 });
    for (const title of ['Agents', 'Host', 'Reviews', 'Pull requests', 'CI runs']) await group(page, title).waitFor();
    assert.match(await group(page, 'Pull requests').textContent(), /org\/app #214.*external/);
    const pr = group(page, 'Pull requests').locator('a');
    assert.equal(await pr.getAttribute('href'), 'https://github.com/org/app/pull/214');
    assert.equal(await pr.getAttribute('rel'), 'noopener noreferrer');
    await group(page, 'Agents').locator('a').first().click();
    await page.waitForURL(/#\/herd\/session\//);
    await page.locator('.rel dl').waitFor({ timeout: 8000 });
    assert.match(await group(page, 'Mission').textContent(), /Alpha/);
    await group(page, 'Host').locator('a').click();
    await page.waitForURL(/#\/hosts\/beast/);
    await group(page, 'Services').waitFor({ timeout: 8000 });
    assert.ok(await group(page, 'Agents').locator('a').count() >= 1);
    assert.ok(await group(page, 'Services').locator('a').count() >= 1);
    await group(page, 'Services').locator('a').first().click();
    await page.waitForURL(/#\/services\//);
    await page.locator('.rel dl').waitFor({ timeout: 8000 });
    await group(page, 'Host').locator('a').waitFor();
  });
});

test('a CI link from the mission opens the CI page filtered to that run', async () => {
  await withApp(async ({ page, base }) => {
    await openAlpha(page, base);
    await group(page, 'CI runs').waitFor({ timeout: 8000 });
    const href = await group(page, 'CI runs').locator('a').getAttribute('href');
    assert.match(href, /^#\/ci\?search=12&repository=7$/);
  });
});

test('a failed related request says so instead of showing nothing', async () => {
  await withApp(async ({ page, base }) => {
    await page.route('**/api/v1/related*', (route) => route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"boom"}' }));
    await page.goto(`${base}/#/hosts/beast`);
    await page.locator('.rel-note', { hasText: 'Related items could not be loaded' }).waitFor({ timeout: 8000 });
  });
});

test('the panel contains itself at phone width and escapes hostile labels', async () => {
  await withApp(async ({ page, base }) => {
    await page.route('**/api/v1/related*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      kind: 'host', id: 'beast', partial: ['ci'], more: { agents: 3 },
      relations: { agents: [{ id: 'a"><img src=x onerror=window.__x=1>', label: '<img src=x onerror=window.__x=1>' + 'w'.repeat(400), state: 'active' }] },
    }) }));
    await page.setViewportSize({ width: 390, height: 800 });
    await page.goto(`${base}/#/hosts/beast`);
    await page.locator('.rel dl').waitFor({ timeout: 8000 });
    assert.equal(await page.evaluate(() => window.__x), undefined);
    assert.equal(await page.locator('.rel img').count(), 0);
    assert.match(await page.locator('.rel').textContent(), /\+3 more/);
    assert.match(await page.locator('.rel').textContent(), /Could not read CI/);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
  });
});

test('the LLM session page, a CI run and a selected project link to their neighbours', async () => {
  await withApp(async ({ page, base }) => {
    const { sessions } = await (await fetch(`${base}/api/v1/sessions`)).json();
    const agent = sessions.find((s) => s.repository_id === 'org/app');
    await page.goto(`${base}/#/llm/session/${encodeURIComponent(agent.session_id)}`);
    await page.locator('.rel dl').waitFor({ timeout: 8000 });
    assert.match(await group(page, 'Mission').textContent(), /Alpha/);
    await page.goto(`${base}/#/ci?repository=7&search=12`);
    await page.locator('h1', { hasText: 'CI' }).waitFor();
    assert.match(await page.locator('.ig-note', { hasText: 'Filtered to' }).first().innerHTML(), /<a href="#\/github\?repository=7">7<\/a>/);
  });
});
