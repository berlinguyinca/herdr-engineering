// Mission page story: result chip, what happened, agents, outcome and cost, the Related panel,
// the fallback when the story is unavailable, and containment at phone width.
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

async function withApp(fn, env = {}) {
  const srv = await startServer({ BURST: '0', MISSION_STORY_SEED: '1', CI_OFFLINE: '1', ...env });
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

async function open(page, base, title) {
  const { missions } = await (await fetch(`${base}/api/v1/missions`)).json();
  const m = missions.find((x) => x.title === title);
  await page.goto(`${base}/#/missions/${encodeURIComponent(m.mission_id)}`);
}

test('an ended mission tells what happened, who did it, how it ended and what it cost', async () => {
  await withApp(async ({ page, base }) => {
    await open(page, base, 'Alpha');
    await page.locator('.ms-chip').waitFor({ timeout: 8000 });
    assert.equal((await page.locator('.ms-chip').textContent()).trim(), 'Succeeded');
    const text = await page.locator('.ms').textContent();
    assert.match(text, /org\/app · fix\/parser · host beast/);
    assert.match(text, /PR #214 open · CI passed/);
    assert.match(text, /Ended: complete/);
    assert.ok(await page.locator('.ms-events li').count() >= 5);
    const times = await page.locator('.ms-events time').evaluateAll((els) => els.map((e) => e.getAttribute('datetime')));
    assert.deepEqual(times, [...times].sort());
    await page.locator('.ms-agent a').first().click();
    await page.waitForURL(/#\/herd\/session\//);
  });
});

test('the story keeps the Related panel and the raw record on the page', async () => {
  await withApp(async ({ page, base }) => {
    await open(page, base, 'Alpha');
    await page.locator('.ms-chip').waitFor({ timeout: 8000 });
    await page.locator('.rel dl').waitFor({ timeout: 8000 });
    await page.locator('details[data-history-panel="events"]').waitFor();
    assert.match(await page.locator('.ms-grid').textContent(), /Pull requests.*org\/app #214/s);
  });
});

test('failed, running and pre-outcome missions are labelled honestly', async () => {
  await withApp(async ({ page, base }) => {
    await open(page, base, 'Gamma');
    await page.locator('.ms-chip').waitFor({ timeout: 8000 });
    assert.equal((await page.locator('.ms-chip').textContent()).trim(), 'Failed');
    await open(page, base, 'Beta');
    await page.locator('.ms-chip', { hasText: 'Running' }).waitFor({ timeout: 8000 });
    await open(page, base, 'Delta');
    await page.locator('.ms-note', { hasText: 'Recorded before outcomes were kept' }).waitFor({ timeout: 8000 });
  });
});

test('when the story cannot be loaded the page still shows the recorded history', async () => {
  await withApp(async ({ page, base }) => {
    await page.route('**/api/v1/missions/*/story', (route) => route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"boom"}' }));
    await open(page, base, 'Alpha');
    await page.locator('details[data-history-panel="brief"]').waitFor({ timeout: 8000 });
    assert.equal(await page.locator('.ms').count(), 0);
  });
});

test('the story contains itself at phone width and escapes hostile text', async () => {
  await withApp(async ({ page, base }) => {
    await page.route('**/api/v1/missions/*/story', async (route) => {
      const real = await (await route.fetch()).json();
      real.objective = '<img src=x onerror=window.__x=1>';
      real.events.push({ ts: '2026-10-24T13:00:00+00:00', kind: 'agent', text: '<img src=x onerror=window.__x=1>' + 'w'.repeat(300), ref: { kind: 'session', id: '"><img src=x onerror=window.__x=1>' } });
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(real) });
    });
    await page.setViewportSize({ width: 390, height: 800 });
    await open(page, base, 'Alpha');
    await page.locator('.ms-chip').waitFor({ timeout: 8000 });
    assert.equal(await page.evaluate(() => window.__x), undefined);
    assert.equal(await page.locator('.ms img').count(), 0);
    const wide = await page.evaluate(() => [...document.querySelectorAll('body *')].filter((e) => e.getBoundingClientRect().right > window.innerWidth + 1).slice(0, 5).map((e) => e.tagName + '.' + e.className));
    assert.deepEqual(wide, []);
    const info = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, iw: window.innerWidth, over: [...document.querySelectorAll('*')].filter((e) => e.scrollWidth > e.clientWidth + 1 && e.clientWidth > 0).slice(0, 8).map((e) => `${e.tagName}.${e.className} ${e.scrollWidth}/${e.clientWidth}`) }));
    assert.ok(info.sw <= info.iw, JSON.stringify(info));
  });
});

test('catalog rows carry the stored result and the result and repository filters narrow them', async () => {
  await withApp(async ({ page, base }) => {
    await page.goto(`${base}/#/missions?tab=finished`);
    await page.locator('[data-mission-key] .mc-result').first().waitFor({ timeout: 8000 });
    const alpha = page.locator('[data-mission-key]', { hasText: 'Alpha' });
    assert.match(await alpha.locator('.mc-result').textContent(), /Succeeded/);
    assert.match(await alpha.textContent(), /Last: PR #214 open/);
    await page.selectOption('#mc-result-filter', 'failed');
    await page.locator('#mc-result', { hasText: '0 matching' }).waitFor();
    assert.equal(await page.locator('[data-mission-key]', { hasText: 'Alpha' }).count(), 0);
    await page.selectOption('#mc-result-filter', 'all');
    await page.selectOption('#mc-repository', 'org/app');
    await page.locator('[data-mission-key]', { hasText: 'Alpha' }).waitFor();
    await page.goto(`${base}/#/missions?tab=failed`);
    await page.locator('[data-mission-key]', { hasText: 'Gamma' }).locator('.mc-result').waitFor({ timeout: 8000 });
    assert.match(await page.locator('[data-mission-key]', { hasText: 'Gamma' }).locator('.mc-result').textContent(), /Failed/);
    await page.setViewportSize({ width: 390, height: 800 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
  });
});
