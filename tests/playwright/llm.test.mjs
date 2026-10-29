// LLM page (#/llm): tiles, charts, the agent and problem tables, the links to the agent,
// and containment at phone width.
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
  const srv = await startServer({ BURST: '0', LLM_SEED: '1', ...env });
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

test('the LLM page shows request, latency, speed and error figures with links to the agent', async () => {
  await withApp(async ({ page, base }) => {
    await page.goto(`${base}/#/llm`);
    await page.locator('.llm-tile').first().waitFor({ timeout: 8000 });
    const tiles = await page.locator('.llm-tile__value').allTextContents();
    assert.equal(tiles[0], '35');                         // 30 streams + 3 failures + 1 unknown session + 1 v1 record
    assert.match(tiles[2], /ms \/ .*ms|ms \/ .* s/);      // TTFT p50 / p95 are real numbers (n >= 5)
    assert.match(tiles[3], /t\/s$/);
    assert.match(tiles[4], /^\d+(\.\d+)?% \(\d+\)$/);
    assert.equal(await page.locator('.llm-card svg').count(), 3);
    await page.locator('.llm-data summary').waitFor();
    const link = page.locator('.llm-section a[href^="#/llm/session/"]').first();
    await link.waitFor();
    await link.click();
    await page.getByText('Open agent').waitFor();
    assert.match(page.url(), /#\/llm\/session\//);
    assert.equal(await page.locator('.llm-tile__value').first().textContent(), '33');   // this agent only: 30 + 3 failures
    await page.getByText('Open agent').click();
    assert.match(page.url(), /#\/herd\/session\//);
    await page.locator('[data-llm-link]').first().waitFor();       // the workspace links to its usage
    await page.goto(`${base}/#/llm`);
    await page.locator('.llm-tile').first().waitFor();
    await page.getByText('upstream_admission').first().waitFor();
    assert.ok(await page.getByText('(unmatched)').count() >= 1);
  });
});

test('switching the window and a filter updates the URL and the figures', async () => {
  await withApp(async ({ page, base }) => {
    await page.goto(`${base}/#/llm`);
    await page.locator('.llm-tile').first().waitFor({ timeout: 8000 });
    await page.locator('a.llm-window', { hasText: '1h' }).click();
    assert.match(page.url(), /#\/llm\?window=1h/);
    await page.locator('a.llm-window[aria-current=page]', { hasText: '1h' }).waitFor();
    assert.equal(await page.locator('a.llm-window[aria-current=page]').textContent(), '1h');
    await page.locator('[data-llm-filter=model]').selectOption('qwen');
    await page.waitForURL(/model=qwen/);
    await page.locator('.llm-tile').first().waitFor();
  });
});

test('the page fits a 390px screen without horizontal scrolling', async () => {
  await withApp(async ({ page, base }) => {
    await page.setViewportSize({ width: 390, height: 800 });
    await page.goto(`${base}/#/llm`);
    await page.locator('.llm-tile').first().waitFor({ timeout: 8000 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok(overflow <= 0, `horizontal overflow ${overflow}px`);
  });
});

test('an empty install shows placeholders, not zeros', async () => {
  await withApp(async ({ page, base }) => {
    await page.goto(`${base}/#/llm`);
    await page.getByText('No usage has been recorded yet.').waitFor({ timeout: 8000 });
    assert.ok(await page.getByText('No requests recorded in this window.').count() >= 1);
  }, { LLM_SEED: '0', INSPECTOR_SEED: '0' });
});
