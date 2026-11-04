// Owner Feedback tab of the artifact share panel against the production routes (artifact-shares-server.py): hostile
// annotation text stays text, the addressed row links to its version, Dismiss sends one POST, a failed read shows the
// fixed error with Retry, and the tab does not scroll sideways at 320px. No frames anywhere on the page.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const ROOT = path.resolve(import.meta.dirname, '../..');
const PYTHON = process.env.PYTHON || (fs.existsSync(path.join(ROOT, '.venv/bin/python')) ? path.join(ROOT, '.venv/bin/python') : 'python3');
const HOSTILE_TAG = '<img src=x onerror=window.__x=1>';

async function withFeedback(callback, env = {}) {
  const server = spawn(PYTHON, [path.join(ROOT, 'tests/playwright/artifact-shares-server.py')], {
    cwd: ROOT, env: { ...process.env, ...env }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let browser, output = '';
  try {
    const port = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('Share server did not start')), 20000);
      server.stdout.on('data', data => { output += data; const match = output.match(/PORT=(\d+)/); if (match) { clearTimeout(timeout); resolve(Number(match[1])); } });
      server.stderr.on('data', data => process.stderr.write(`[feedback-server] ${data}`));
      server.once('exit', code => { clearTimeout(timeout); reject(new Error(`Share server exited ${code}`)); });
    });
    const options = { headless: true, args: ['--no-sandbox'] };
    if (process.env.PLAYWRIGHT_CHROMIUM) options.executablePath = process.env.PLAYWRIGHT_CHROMIUM;
    try { browser = await chromium.launch(options); }
    catch (error) {
      const fallback = '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
      if (!fs.existsSync(fallback)) throw error;
      browser = await chromium.launch({ ...options, executablePath: fallback });
    }
    const base = `http://127.0.0.1:${port}`;
    const context = await browser.newContext();
    const page = await context.newPage();
    const requests = [];
    page.on('request', request => requests.push({ url: request.url(), method: request.method() }));
    page.on('dialog', dialog => dialog.accept());          // Dismiss asks confirm()
    await callback({ page, base, requests });
  } finally {
    await browser?.close();
    server.kill();
  }
}

const feedback = page => page.locator('[data-feedback-panel]');
const rows = page => feedback(page).locator('[data-feedback-row]');

async function openFeedback(page, base, id = 'web_1') {
  await page.goto(`${base}/#/artifacts/${id}`);
  await page.locator('[data-panel-tab="feedback"]').click();
  await page.locator('[data-panel-pane="feedback"]').waitFor({ state: 'visible' });
  await feedback(page).waitFor();
}

const settled = page => feedback(page).locator('[data-feedback-filter]').waitFor();
// The list is empty while it reloads, so "the row is gone" alone is not "the reload finished": wait for the final count.
async function untilRows(page, count) {
  for (let i = 0; i < 100; i++) {
    if (await rows(page).count() === count && await feedback(page).locator('[role="status"]').count() === 0) return;
    await page.waitForTimeout(50);
  }
  assert.equal(await rows(page).count(), count);
}
const dismissPosts = requests => requests.filter(r => r.method === 'POST' && /\/annotations\/ann_[^/]+\/dismiss$/.test(r.url));
const noFrames = async page => {
  assert.equal(await page.locator('iframe, frame, object, embed').count(), 0);
  assert.equal(page.frames().length, 1);
};

test('open feedback shows the hostile body as literal text, the edited marker, and no addressed row', async () => {
  await withFeedback(async ({ page, base, requests }) => {
    await openFeedback(page, base);
    await rows(page).first().waitFor();
    assert.equal(await rows(page).count(), 2);                         // open: hostile + edited; the addressed one is filtered out
    const text = await feedback(page).innerText();
    assert.ok(text.includes(HOSTILE_TAG), 'the hostile markup is shown as text');
    assert.match(await rows(page).filter({ hasText: 'typo in the heading' }).innerText(), /edited/);
    assert.doesNotMatch(await rows(page).filter({ hasText: 'hostile' }).innerText(), /edited/);
    assert.equal(await page.evaluate(() => window.__x), undefined);
    assert.equal(await page.locator('img[src="x"]').count(), 0);
    assert.equal(await feedback(page).locator('img').count(), 0);
    assert.ok(requests.some(r => r.method === 'GET' && /\/api\/v1\/artifacts\/web_1\/annotations\?status=open/.test(r.url)));
    await noFrames(page);
  });
});

test('the addressed row links to the version that fixed it', async () => {
  await withFeedback(async ({ page, base }) => {
    await openFeedback(page, base);
    await feedback(page).locator('[data-feedback-filter]').selectOption('addressed');
    await rows(page).filter({ hasText: 'fixed in the third revision' }).waitFor();
    assert.equal(await rows(page).count(), 1);
    const link = rows(page).first().locator('a', { hasText: 'addressed in v3' });
    assert.match(await link.getAttribute('href'), /^#\/artifacts\/web_1\?version=version_3$/);
    assert.equal(await rows(page).first().locator('[data-feedback-action="dismiss"]').count(), 0);
  });
});

test('Dismiss moves the row from Open to Dismissed', async () => {
  await withFeedback(async ({ page, base, requests }) => {
    await openFeedback(page, base);
    await rows(page).first().waitFor();
    await rows(page).filter({ hasText: 'typo in the heading' }).locator('[data-feedback-action="dismiss"]').click();
    await untilRows(page, 1);
    assert.equal(await rows(page).filter({ hasText: 'typo in the heading' }).count(), 0);
    assert.equal(dismissPosts(requests).length, 1);
    await feedback(page).locator('[data-feedback-filter]').selectOption('dismissed');
    await rows(page).filter({ hasText: 'typo in the heading' }).waitFor();
    assert.equal(await rows(page).count(), 1);
    assert.match(await rows(page).first().innerText(), /dismissed/);
  });
});

test('a double click on Dismiss sends one POST', async () => {
  await withFeedback(async ({ page, base, requests }) => {
    await openFeedback(page, base);
    await rows(page).first().waitFor();
    const button = rows(page).filter({ hasText: 'typo in the heading' }).locator('[data-feedback-action="dismiss"]');
    await button.dblclick({ force: true });
    await untilRows(page, 1);
    assert.equal(await rows(page).filter({ hasText: 'typo in the heading' }).count(), 0);
    assert.equal(dismissPosts(requests).length, 1);
  }, { DISMISS_DELAY: '0.4' });
});

test('a failed read shows the fixed error with Retry, and Retry recovers', async () => {
  await withFeedback(async ({ page, base, requests }) => {
    await openFeedback(page, base);
    const retry = feedback(page).locator('[data-feedback-action="retry"]');
    await retry.waitFor();
    const text = await feedback(page).innerText();
    assert.match(text, /Feedback is unavailable right now\./);
    assert.doesNotMatch(text, /secret_db_user|password|authentication/i);   // the driver text never reaches the page
    assert.ok(requests.some(r => /annotations\?status=open/.test(r.url)));
    await retry.click();
    await rows(page).first().waitFor();
    assert.equal(await rows(page).count(), 2);
    assert.equal(await retry.count(), 0);
  }, { ANNOT_FAIL_FIRST: '1' });
});

test('at 320px the Feedback tab has no horizontal scroll', async () => {
  await withFeedback(async ({ page, base }) => {
    await page.setViewportSize({ width: 320, height: 800 });
    await openFeedback(page, base);
    await rows(page).first().waitFor();
    for (const status of ['open', 'all']) {
      await feedback(page).locator('[data-feedback-filter]').selectOption(status);
      await settled(page);
      await rows(page).first().waitFor();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert.ok(overflow <= 0, `page scrolls sideways by ${overflow}px`);
    }
  });
});
