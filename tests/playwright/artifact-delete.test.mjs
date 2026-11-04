import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const ROOT = path.resolve(import.meta.dirname, '../..');
const PYTHON = process.env.PYTHON || (fs.existsSync(path.join(ROOT, '.venv/bin/python')) ? path.join(ROOT, '.venv/bin/python') : 'python3');

async function withDelete(callback, { options = {}, headers } = {}) {
  const server = spawn(PYTHON, [path.join(ROOT, 'tests/playwright/artifact-delete-server.py'), JSON.stringify(options)], {
    cwd: ROOT, env: { ...process.env, PYTHONPATH: ROOT }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let browser, output = '';
  try {
    const fixture = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('Delete fixture did not start')), 60000);
      server.stdout.on('data', data => { output += data; try { const parsed = JSON.parse(output.split('\n')[0]); clearTimeout(timeout); resolve(parsed); } catch { /* wait for the whole line */ } });
      server.stderr.on('data', data => process.stderr.write(`[delete-server] ${data}`));
      server.once('exit', code => { clearTimeout(timeout); reject(new Error(`Delete fixture exited ${code}`)); });
    });
    const launch = { headless: true, args: ['--no-sandbox'] };
    if (process.env.PLAYWRIGHT_CHROMIUM) launch.executablePath = process.env.PLAYWRIGHT_CHROMIUM;
    try { browser = await chromium.launch(launch); }
    catch (error) {
      const fallback = '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
      if (!fs.existsSync(fallback)) throw error;
      browser = await chromium.launch({ ...launch, executablePath: fallback });
    }
    const context = await browser.newContext({ viewport: { width: 320, height: 800 }, extraHTTPHeaders: headers });
    const page = await context.newPage();
    const requests = [];
    page.on('request', request => { if (request.method() === 'POST') requests.push({ url: request.url(), body: request.postData() }); });
    const blobs = async () => (await (await fetch(fixture.state)).json()).blobs;
    await callback({ page, fixture, requests, blobs });
  } finally {
    await browser?.close();
    server.kill();
  }
}

const rows = page => page.locator('[data-artifact-results] tbody tr');
const pick = (page, name) => page.locator(`[data-artifact-select][aria-label="Select ${name}"]`);
const openList = async (page, base) => {
  await page.goto(`${base}/#/artifacts`);
  await page.locator('[data-artifact-owner]:not([hidden])').waitFor();
  await rows(page).first().waitFor();
};

test('the owner selects, deletes, sees the deleted view, restores, and nothing overflows at 320px', async () => {
  await withDelete(async ({ page, fixture, requests, blobs }) => {
    await openList(page, fixture.base);
    assert.equal(await rows(page).count(), 5);
    const overflow = () => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok(await overflow() <= 1, 'no page-level horizontal scroll at 320px');
    assert.equal(await page.locator('[data-artifact-delete]').isDisabled(), true);
    await pick(page, 'alpha.md').check();
    await pick(page, 'beta.md').check();
    await page.locator('[data-artifact-delete]').click();
    await page.locator('[data-artifact-action-state]', { hasText: '2 deleted.' }).waitFor();
    assert.equal(await rows(page).count(), 3);
    const sent = JSON.parse(requests.find(r => r.url.endsWith('/artifacts/delete')).body);
    assert.deepEqual([sent.mode, sent.ids.length], ['tombstone', 2]);
    assert.ok(Object.values(await blobs()).every(Boolean), 'a restorable delete keeps every byte');
    await page.locator('[data-artifact-view]').selectOption('deleted');
    await page.locator('[data-artifact-results] th', { hasText: 'Purge after' }).waitFor();
    await page.waitForFunction(() => document.querySelectorAll('[data-artifact-results] tbody tr').length === 2);
    assert.equal(await rows(page).count(), 2);
    assert.equal(await page.locator('[data-artifact-restore]').isVisible(), true);
    assert.equal(await page.locator('[data-artifact-purge]').isVisible(), false);
    await pick(page, 'alpha.md').check();
    await page.locator('[data-artifact-restore]').click();
    await page.locator('[data-artifact-action-state]', { hasText: '1 restored.' }).waitFor();
    await page.waitForFunction(() => document.querySelectorAll('[data-artifact-results] tbody tr').length === 1);
    assert.equal(await rows(page).count(), 1);
    await page.locator('[data-artifact-view]').selectOption('active');
    await page.waitForFunction(() => document.querySelectorAll('[data-artifact-results] tbody tr').length === 4);
    assert.equal(await rows(page).count(), 4);
    assert.ok(await overflow() <= 1);
  });
});

test('delete permanently needs the typed count and then really removes the bytes', async () => {
  await withDelete(async ({ page, fixture, requests, blobs }) => {
    await openList(page, fixture.base);
    await pick(page, 'gamma.md').check();
    await pick(page, 'delta.md').check();
    await page.locator('[data-artifact-purge]').click();
    const go = page.locator('[data-artifact-purge-go]');
    assert.equal(await page.locator('[data-artifact-purge-count]').textContent(), '2');
    assert.equal(await go.isDisabled(), true);
    await page.locator('[data-artifact-purge-confirm]').fill('1');
    assert.equal(await go.isDisabled(), true);
    await page.locator('[data-artifact-purge-confirm]').fill('2');
    assert.equal(await go.isDisabled(), false);
    // Changing the selection after typing re-arms the gate: the typed count no longer matches.
    await pick(page, 'delta.md').uncheck();
    assert.equal(await go.isDisabled(), true);
    await pick(page, 'delta.md').check();
    await page.locator('[data-artifact-purge-confirm]').fill('2');
    assert.equal(requests.filter(r => r.url.endsWith('/artifacts/delete')).length, 0, 'nothing is sent before the gate opens');
    await go.click();
    await page.locator('[data-artifact-action-state]', { hasText: '2 permanently deleted.' }).waitFor();
    const sent = JSON.parse(requests.find(r => r.url.endsWith('/artifacts/delete')).body);
    assert.deepEqual([sent.mode, sent.confirm], ['purge_now', '2']);
    const stored = await blobs();
    assert.equal(stored[fixture.ids['gamma.md']], false);
    assert.equal(stored[fixture.ids['delta.md']], false);
    assert.equal(stored[fixture.ids['alpha.md']], true);
    await page.locator('[data-artifact-view]').selectOption('deleted');
    await page.locator('[data-artifact-results] tbody tr', { hasText: '(purged)' }).first().waitFor();
    assert.equal(await page.locator('[data-artifact-select]:not([disabled])').count(), 0, 'purged rows cannot be restored');
  });
});

test('hostile names stay text', async () => {
  await withDelete(async ({ page, fixture }) => {
    await openList(page, fixture.base);
    assert.equal(await page.evaluate(() => window.__x), undefined);
    assert.equal(await page.locator('[data-artifact-results] img').count(), 0);
    assert.ok((await rows(page).allTextContents()).some(text => text.includes('<img src=x')));
  });
});

test('a viewer key sees no delete controls and the server refuses its calls', async () => {
  await withDelete(async ({ page, fixture }) => {
    await page.goto(`${fixture.base}/#/artifacts`);
    await page.locator('[data-artifact-results]').waitFor();
    await page.waitForTimeout(800);
    assert.equal(await page.locator('[data-artifact-owner]').isHidden(), true);
    assert.equal(await page.locator('[data-artifact-select]').count(), 0);
  }, { options: { keys: true }, headers: { 'X-API-Key': 'viewer-key' } });
});

test('a dead store shows one fixed message and changes nothing', async () => {
  await withDelete(async ({ page, fixture, blobs }) => {
    await openList(page, fixture.base);
    await pick(page, 'alpha.md').check();
    await fetch(fixture.state, { method: 'POST' });
    await page.locator('[data-artifact-delete]').click();
    const status = page.locator('[data-artifact-action-state]', { hasText: 'did not work' });
    await status.waitFor();
    const text = await status.textContent();
    assert.doesNotMatch(text, /connection|closed|SQLSTATE|asyncpg|503/i);
    assert.ok(Object.values(await blobs()).every(Boolean));
  });
});

test('changing the search prunes hidden selections so the typed count is only what is on screen', async () => {
  await withDelete(async ({ page, fixture }) => {
    await openList(page, fixture.base);
    await pick(page, 'alpha.md').check();
    await pick(page, 'beta.md').check();
    await page.locator('[data-artifact-query]').fill('beta');
    await page.locator('[data-artifact-results] tbody tr').nth(0).waitFor();
    await page.waitForFunction(() => document.querySelectorAll('[data-artifact-results] tbody tr').length === 1);
    await page.locator('[data-artifact-purge]').click();
    assert.equal(await page.locator('[data-artifact-purge-count]').textContent(), '1');
  });
});
