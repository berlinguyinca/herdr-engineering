// Playwright UI tests for the control-plane SPA (Spec §91 loading/error states).
//
// These cover the regression reported as "Failed to fetch / too many requests"
// on Overview, Activity, Analytics, etc. The SPA re-renders (re-fetching all
// data for the view) on SSE events; an un-throttled refresh trips the API rate
// limiter (429 "too many requests") and exhausts the HTTP/1.1 connection pool
// ("Failed to fetch"). The server fixture (server.py) streams gapped event
// bursts so this path is exercised under real load. Each test uses a fresh
// server + browser so the per-IP rate budget (60s window) is not shared across
// tests, keeping the assertions deterministic.
//
// Run: npm run test:ui   (or node --test tests/playwright/ui.test.mjs)
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
// Playwright resolves its own bundled browser (matching the pinned
// devDependency). On dev boxes whose cached browser build lags the installed
// Playwright version (this host has chromium-1243 while Playwright 1.64 wants
// headless_shell-1248), fall back to a locally-pinned chromium. Override with
// PLAYWRIGHT_CHROMIUM to force a specific executable.
const EXE_FALLBACK = process.env.PLAYWRIGHT_CHROMIUM
  || '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';

const ROUTES = [
  ['/overview', 'Overview'],
  ['/activity', 'Activity'],
  ['/analytics', 'Analytics'],
  ['/missions', 'Alpha'],
  ['/herd', 'Sessions'],
  ['/hosts', 'beast'],
  ['/services', 'web service on beast'],
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function startServer(env = {}) {
  const proc = spawn(PY, [path.join(REPO, 'tests', 'playwright', 'server.py')], {
    env: { ...process.env, ...env },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const port = await new Promise((resolve, reject) => {
    let buf = '';
    const timer = setTimeout(() => reject(new Error('server start timeout')), 20000);
    proc.stdout.on('data', (d) => {
      buf += d.toString();
      let m;
      while ((m = buf.match(/PORT=(\d+)/))) {
        clearTimeout(timer);
        resolve(Number(m[1]));
        buf = buf.slice(m.index + m[0].length);
      }
    });
    proc.on('exit', (c) => reject(new Error(`server exited early (${c})`)));
    proc.stderr.on('data', (d) => process.stderr.write(`[srv] ${d}`));
  });
  return { proc, port };
}

async function launchBrowser() {
  const args = ['--no-sandbox'];
  if (process.env.PLAYWRIGHT_CHROMIUM) {
    return chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM, args });
  }
  try {
    return await chromium.launch({ headless: true, args }); // auto-detect bundled browser
  } catch (err) {
    if (fs.existsSync(EXE_FALLBACK)) {
      return chromium.launch({ headless: true, executablePath: EXE_FALLBACK, args });
    }
    throw err;
  }
}

// Start a fresh server + browser, run `fn`, then tear both down. A fresh
// server per test isolates the API rate budget (60s sliding window) so tests
// are deterministic.
async function withApp(fn, env = {}) {
  const srv = await startServer(env);
  let browser;
  try {
    browser = await launchBrowser();
    const page = await browser.newPage();
    const base = `http://127.0.0.1:${srv.port}`;
    return await fn({ page, base });
  } finally {
    if (browser) { try { await browser.close(); } catch { /* noop */ } }
    try { srv.proc.kill(); } catch { /* noop */ }
  }
}

const bodyText = (page) =>
  page.evaluate(() => document.body ? document.body.innerText : '');

test('every page renders without rate-limit / fetch errors', async () => {
  // No event burst (BURST=0): the SPA renders once per view with no SSE churn,
  // so content presence is deterministic and never races a live re-render.
  await withApp(async ({ page, base }) => {
    const pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(`pageerror: ${e.message}`));
    for (const [route, needle] of ROUTES) {
      await page.goto(`${base}/#${route}`);
      // waitForFunction is the authoritative render check (the needle must
      // actually appear). A view's own mount re-render can briefly show a
      // spinner immediately after, so we do NOT re-assert the needle here --
      // we only check for error cards, which never appear during a normal
      // (non-burst) render.
      await page.waitForFunction(
        (n) => (document.body ? document.body.innerText : '').includes(n),
        needle, { timeout: 15000 });
      const text = await bodyText(page);
      assert.ok(!text.includes('too many requests'),
        `rate-limit error on ${route}:\n${text.slice(0, 400)}`);
      assert.ok(!text.includes('Failed to fetch'),
        `fetch error on ${route}:\n${text.slice(0, 400)}`);
    }
    assert.deepEqual(pageErrors, [], 'no uncaught page exceptions expected');
  }, { BURST: '0' });
});

test('overview survives a heavy event burst without rate limiting', async () => {
  await withApp(async ({ page, base }) => {
    await page.goto(`${base}/#/overview`);
    await page.waitForFunction(
      () => (document.body ? document.body.innerText : '').includes('Overview'),
      null, { timeout: 15000 });
    // The throttled live refresh re-renders (and briefly shows the spinner),
    // so we only assert that NO error card ever appears during the burst and
    // that the overview recovers afterwards — never that the word "Overview"
    // is present at every instant.
    const start = Date.now();
    let sawError = null;
    while (Date.now() - start < 15000) {
      await sleep(500);
      const text = await bodyText(page);
      if (text.includes('too many requests')) { sawError = 'too many requests'; break; }
      if (text.includes('Failed to fetch')) { sawError = 'Failed to fetch'; break; }
    }
    assert.ok(!sawError, `rate-limit/fetch error appeared during burst: ${sawError}`);
    // The view must recover (not be stuck on an error card).
    await page.waitForFunction(
      () => (document.body ? document.body.innerText : '').includes('Overview'),
      null, { timeout: 10000 });
  });
});
