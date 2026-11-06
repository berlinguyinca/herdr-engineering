// Real Chromium + real control plane: the report Rich view renders the author's page in an inert sandboxed frame.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const ROOT = path.resolve(import.meta.dirname, '../..');
const PYTHON = process.env.PYTHON || (fs.existsSync(path.join(ROOT, '.venv/bin/python')) ? path.join(ROOT, '.venv/bin/python') : 'python3');
const SHOTS = process.env.RICH_SHOT_DIR || '';

async function withReports(callback, env = {}, pageOptions = {}) {
  const server = spawn(PYTHON, [path.join(ROOT, 'tests/playwright/artifact-reports-server.py')], {
    cwd: ROOT, env: { ...process.env, REPORT_RICH: '1', ...env }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let browser;
  try {
    const port = await new Promise((resolve, reject) => {
      let output = '';
      const timeout = setTimeout(() => reject(new Error('Report server did not start')), 20000);
      server.stdout.on('data', data => { output += data; const match = output.match(/PORT=(\d+)/); if (match) { clearTimeout(timeout); resolve(Number(match[1])); } });
      server.stderr.on('data', data => process.stderr.write(`[report-server] ${data}`));
      server.once('exit', code => { clearTimeout(timeout); reject(new Error(`Report server exited ${code}`)); });
    });
    const options = { headless: true, args: ['--no-sandbox'] };
    if (process.env.PLAYWRIGHT_CHROMIUM) options.executablePath = process.env.PLAYWRIGHT_CHROMIUM;
    browser = await chromium.launch(options);
    const page = await browser.newPage(pageOptions);
    await callback({ page, base: `http://127.0.0.1:${port}` });
  } finally {
    await browser?.close();
    server.kill();
  }
}

async function openRich(page, base) {
  await page.goto(`${base}/#/artifacts/report_1`);
  await page.locator('[data-report-version]').waitFor();
  await page.getByText('Finding from version 2.', { exact: true }).waitFor();
  assert.equal(await page.locator('iframe').count(), 0, 'no frame until asked');
  await page.getByRole('button', { name: 'Show rich view', exact: true }).click();
  const frameElement = page.locator('iframe[data-rich-frame]');
  await frameElement.waitFor();
  const frame = await (await frameElement.elementHandle()).contentFrame();
  await frame.getByRole('heading', { name: 'Candidate cuts p95 latency by 18%' }).waitFor();
  return { frameElement, frame };
}

test('the author page renders, scripts do not run, and the frame cannot reach the dashboard', async () => {
  await withReports(async ({ page, base }) => {
    const { frameElement, frame } = await openRich(page, base);
    assert.equal(await frameElement.getAttribute('sandbox'), '');
    assert.match(await frameElement.getAttribute('src'), /\/api\/v1\/artifacts\/report_1\/versions\/version_2\/rich$/);
    assert.equal(await page.locator('iframe').count(), 1);
    // the author's text is visible and styled by its own CSS
    await frame.getByText('312 ms').waitFor();
    // the page's <script> would have rewritten this; it did not run
    assert.equal(await frame.locator('#probe').textContent(), 'Scripts have not run.');
    // an opaque origin: the frame is not the dashboard's origin, and the dashboard cannot read into it
    assert.equal(await frame.evaluate(() => self.origin), 'null');   // opaque (location.origin would echo the URL's origin)
    const reach = await page.evaluate(() => {
      const f = document.querySelector('iframe[data-rich-frame]');
      try { return f.contentDocument === null ? 'null-document' : 'readable'; } catch (error) { return error.name; }
    });
    assert.notEqual(reach, 'readable');
    assert.equal(await page.evaluate(() => document.title.includes('pwned')), false);
    assert.notEqual(await page.title(), 'pwned');
    // the frame's own view of its parent: cross-origin access throws
    const parentAccess = await frame.evaluate(() => { try { return typeof window.parent.document.title; } catch (error) { return error.name; } });
    assert.equal(parentAccess, 'SecurityError');
    // the sandbox is enforced by the response too, not only by the attribute
    const response = await page.request.get(new URL(await frameElement.getAttribute('src'), page.url()).href);
    assert.match(response.headers()['content-security-policy'], /^sandbox;/);
    assert.doesNotMatch(response.headers()['content-security-policy'], /allow-/);
    // the honest hint and the way out
    await page.getByText('Scripts are disabled in this view; open the file for the interactive version').waitFor();
    assert.match(await page.locator('[data-rich-open]').getAttribute('href'), /\/api\/v1\/artifacts\/report_1\/live$/);
    // hiding removes the frame again
    await page.getByRole('button', { name: 'Hide rich view', exact: true }).click();
    assert.equal(await page.locator('iframe').count(), 0);
  });
});

test('the sanitised preview is still the default and is unchanged by the toggle', async () => {
  await withReports(async ({ page, base }) => {
    await page.goto(`${base}/#/artifacts/report_1`);
    await page.locator('[data-report-version]').waitFor();
    await page.getByRole('button', { name: 'Load lightweight preview', exact: true }).click();
    await page.locator('[data-static-preview]').waitFor();
    assert.equal(await page.locator('iframe').count(), 0);
  });
});

test('a report without an HTML asset has no Rich view', async () => {
  await withReports(async ({ page, base }) => {
    await page.goto(`${base}/#/artifacts/report_1`);
    await page.locator('[data-report-version]').waitFor();
    await page.getByText('Finding from version 2.', { exact: true }).waitFor();
    assert.equal(await page.locator('[data-rich-view]').count(), 0);
  }, { REPORT_RICH: '' });
});

for (const [name, scheme, viewport] of [['light-1100', 'light', { width: 1100, height: 900 }], ['dark-390', 'dark', { width: 390, height: 844 }]]) {
  test(`rich view screenshot ${name}`, async () => {
    await withReports(async ({ page, base }) => {
      await page.addInitScript(theme => { try { localStorage.setItem('herdr.theme', theme); } catch { /* storage may be blocked */ } }, scheme);
      const { frameElement } = await openRich(page, base);
      await page.locator('[data-rich-view]').scrollIntoViewIfNeeded();
      await frameElement.scrollIntoViewIfNeeded();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert.ok(overflow <= 1, `horizontal overflow ${overflow}px`);
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `rich-${name}.png`), fullPage: false });
    }, {}, { viewport, colorScheme: scheme });
  });
}
