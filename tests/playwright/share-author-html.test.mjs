// Real browser + the production share routes: the Share card's "Include the author's HTML page (Rich view)" option.
// The fixture is artifact-shares-server.py (report_1: report_v1 has no HTML asset, report_v2 has exactly one).
// BROWSERS=chromium,firefox (default chromium,firefox); a missing engine is skipped.
//
//   PYTHON=<venv python> node --test tests/playwright/share-author-html.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import * as playwright from 'playwright';

const ROOT = path.resolve(import.meta.dirname, '../..');
const PYTHON = process.env.PYTHON || (fs.existsSync(path.join(ROOT, '.venv/bin/python')) ? path.join(ROOT, '.venv/bin/python') : 'python3');
const WANTED = (process.env.BROWSERS || 'chromium,firefox').split(',').map(b => b.trim()).filter(Boolean);
const SENTINEL = 'preview.html+author-html';
const BOX = '[data-share-field="includeAuthorHtml"]';

async function withFixture(engine, callback) {
  const server = spawn(PYTHON, [path.join(ROOT, 'tests/playwright/artifact-shares-server.py')], { cwd: ROOT, env: process.env, stdio: ['ignore', 'pipe', 'pipe'] });
  let browser, output = '';
  try {
    const port = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('Share server did not start')), 30000);
      server.stdout.on('data', data => { output += data; const match = output.match(/PORT=(\d+)/); if (match) { clearTimeout(timeout); resolve(Number(match[1])); } });
      server.stderr.on('data', data => process.stderr.write(`[share-server] ${data}`));
      server.once('exit', code => { clearTimeout(timeout); reject(new Error(`Share server exited ${code}`)); });
    });
    const options = { headless: true };
    if (engine === 'chromium') {
      options.args = ['--no-sandbox'];
      if (process.env.PLAYWRIGHT_CHROMIUM) options.executablePath = process.env.PLAYWRIGHT_CHROMIUM;
    }
    try { browser = await playwright[engine].launch(options); } catch (error) { return { skipped: `${engine} is not installed: ${String(error.message).split('\n')[0]}` }; }
    const page = await browser.newPage();
    const requests = [];
    page.on('request', request => requests.push({ url: request.url(), method: request.method(), body: request.postData() }));
    await callback({ page, base: `http://127.0.0.1:${port}`, requests });
    return {};
  } finally {
    await browser?.close();
    server.kill();
  }
}

const panel = page => page.locator('[data-share-panel]');
async function openReport(page, base) {
  await page.goto(`${base}/#/artifacts/report_1`);
  await panel(page).waitFor();
  await panel(page).locator('[data-share-list-region]').filter({ hasNotText: 'Loading links…' }).waitFor();
  await panel(page).locator('[data-share-field="audience"]').selectOption('public');
}
const created = requests => requests.filter(r => r.method === 'POST' && r.url.endsWith('/api/v1/artifacts/report_1/shares'));

for (const engine of WANTED) {
  test(`${engine}: the option appears only for a version with one HTML page, sends the field only when checked, and never shows the sentinel`, async t => {
    const result = await withFixture(engine, async ({ page, base, requests }) => {
      await openReport(page, base);
      // The head (report_v1) has no HTML asset: no checkbox, and the field is absent from the request.
      assert.equal(await panel(page).locator(BOX).count(), 0);
      await panel(page).locator('[data-share-action="review"]').click();
      await panel(page).locator('[data-share-review] [data-share-files]').waitFor();
      assert.equal('include_author_html' in JSON.parse(created(requests)[0].body), false);
      assert.equal(await panel(page).locator('[data-share-review] [data-share-files] code').allInnerTexts().then(a => a.join()), 'preview.html');

      // report_v2 has one: the box appears, unchecked, labelled, with the caution; keyboard operable.
      await panel(page).locator('[data-share-field="versionId"]').selectOption('report_v2');
      const box = panel(page).locator(BOX);
      await box.waitFor();
      assert.equal(await box.isChecked(), false);
      await page.getByLabel("Include the author's HTML page (Rich view)").waitFor();
      await panel(page).getByText('Anyone with the link can view this page. It is shown without scripts.').waitFor();
      await box.focus();
      await page.keyboard.press('Space');
      assert.equal(await box.isChecked(), true);

      await panel(page).locator('[data-share-action="review"]').click();
      const review = panel(page).locator('[data-share-review]');
      await review.locator('code', { hasText: 'author-page.html' }).waitFor();
      assert.deepEqual(JSON.parse(created(requests)[1].body).include_author_html, true);
      await review.locator('[data-share-author-badge]').waitFor();
      assert.match(await review.innerText(), /anyone holding the link can view it/);
      assert.match(await review.innerText(), /opens preview\.html/);

      // The pending row after the refresh: real entry plus badge, never the internal value.
      const row = panel(page).locator('[data-share-row]').first();
      await row.locator('[data-share-author-badge]').waitFor();
      assert.equal((await page.content()).includes(SENTINEL), false);
      assert.equal((await panel(page).innerText()).includes('author-html'), false);

      // Unchecking again after switching to a version without a page drops the box and the field.
      await panel(page).locator('[data-share-field="versionId"]').selectOption('report_v1');
      await box.waitFor({ state: 'detached' });
      assert.equal(await page.locator('iframe').count(), 0);
    });
    if (result.skipped) t.skip(result.skipped);
  });
}
