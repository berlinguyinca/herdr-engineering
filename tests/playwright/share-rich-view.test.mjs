// Real Chromium + real owner API + real share host: an opted-in report share frames the author's page inertly.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const ROOT = path.resolve(import.meta.dirname, '../..');
const PYTHON = process.env.PYTHON || (fs.existsSync(path.join(ROOT, '.venv/bin/python')) ? path.join(ROOT, '.venv/bin/python') : 'python3');
const SHOTS = process.env.RICH_SHOT_DIR || '';

async function withShare(callback, pageOptions = {}) {
  const server = spawn(PYTHON, [path.join(ROOT, 'tests/playwright/share-rich-server.py')], {
    cwd: ROOT, env: process.env, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let browser;
  try {
    const { port, link } = await new Promise((resolve, reject) => {
      let output = '';
      const timeout = setTimeout(() => reject(new Error('Share fixture did not start')), 30000);
      server.stdout.on('data', data => {
        output += data;
        const p = output.match(/PORT=(\d+)/); const l = output.match(/PATH=(\S+)/);
        if (p && l) { clearTimeout(timeout); resolve({ port: Number(p[1]), link: l[1] }); }
      });
      server.stderr.on('data', data => process.stderr.write(`[share-rich] ${data}`));
      server.once('exit', code => { clearTimeout(timeout); reject(new Error(`Share fixture exited ${code}`)); });
    });
    const options = { headless: true, args: ['--no-sandbox'] };
    if (process.env.PLAYWRIGHT_CHROMIUM) options.executablePath = process.env.PLAYWRIGHT_CHROMIUM;
    browser = await chromium.launch(options);
    const page = await browser.newPage(pageOptions);
    await callback({ page, url: `http://127.0.0.1:${port}${link}f/preview.html`, origin: `http://127.0.0.1:${port}` });
  } finally {
    await browser?.close();
    server.kill();
  }
}

async function openRich(page, url) {
  await page.goto(url);
  const toggle = page.getByRole('button', { name: 'Show rich view', exact: true });
  await toggle.waitFor();
  assert.equal(await page.locator('iframe').count(), 0, 'no frame until asked');
  await toggle.click();
  const frameElement = page.locator('iframe[data-rich-frame]');
  await frameElement.waitFor();
  const frame = await (await frameElement.elementHandle()).contentFrame();
  await frame.getByRole('heading', { name: 'Candidate cuts p95 latency by 18%' }).waitFor();
  return { frameElement, frame };
}

test('the author page renders, its script does not run, and the frame cannot reach the share page', async () => {
  await withShare(async ({ page, url, origin }) => {
    const { frameElement, frame } = await openRich(page, url);
    assert.equal(await frameElement.getAttribute('sandbox'), '');
    assert.equal(await frameElement.getAttribute('src'), 'author-page.html');
    assert.equal(await page.locator('iframe').count(), 1);
    await frame.getByText('312 ms').waitFor();
    assert.equal(await frame.locator('#probe').textContent(), 'Scripts have not run.');
    assert.equal(await frame.evaluate(() => self.origin), 'null');
    const parentAccess = await frame.evaluate(() => { try { return typeof window.parent.document.title; } catch (error) { return error.name; } });
    assert.equal(parentAccess, 'SecurityError');
    assert.notEqual(await page.title(), 'pwned');
    const response = await page.request.get(new URL('author-page.html', url).href);
    assert.match(response.headers()['content-security-policy'], /^sandbox;/);
    assert.doesNotMatch(response.headers()['content-security-policy'], /allow-/);
    assert.equal(response.headers()['x-frame-options'], 'SAMEORIGIN');
    await page.getByText('Scripts are disabled in this view', { exact: true }).waitFor();
    // directly navigated, the page is still inert
    await page.goto(new URL('author-page.html', url).href);
    assert.equal(await page.locator('#probe').textContent(), 'Scripts have not run.');
    assert.ok(origin);
  });
});

test('the preview is the default and the toggle hides the frame again', async () => {
  await withShare(async ({ page, url }) => {
    await openRich(page, url);
    await page.getByRole('button', { name: 'Hide rich view', exact: true }).click();
    assert.equal(await page.locator('iframe').count(), 0);
  });
});

for (const [name, scheme, viewport] of [['light-1100', 'light', { width: 1100, height: 900 }], ['dark-390', 'dark', { width: 390, height: 844 }]]) {
  test(`share rich view screenshot ${name}`, async () => {
    await withShare(async ({ page, url }) => {
      const { frameElement } = await openRich(page, url);
      await page.locator('[data-rich-view]').scrollIntoViewIfNeeded();
      await frameElement.scrollIntoViewIfNeeded();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert.ok(overflow <= 1, `horizontal overflow ${overflow}px`);
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `share-rich-${name}.png`), fullPage: false });
    }, { viewport, colorScheme: scheme });
  });
}
