import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const ROOT = path.resolve(import.meta.dirname, '../..');
const PYTHON = process.env.PYTHON || (fs.existsSync(path.join(ROOT, '.venv/bin/python')) ? path.join(ROOT, '.venv/bin/python') : 'python3');

async function withMembers(callback, { key = 'op-key' } = {}) {
  const server = spawn(PYTHON, [path.join(ROOT, 'tests/playwright/artifact-members-server.py')], {
    cwd: ROOT, env: process.env, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let browser, output = '';
  try {
    const port = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('Members server did not start')), 20000);
      server.stdout.on('data', data => { output += data; const match = output.match(/PORT=(\d+)/); if (match) { clearTimeout(timeout); resolve(Number(match[1])); } });
      server.stderr.on('data', data => process.stderr.write(`[members-server] ${data}`));
      server.once('exit', code => { clearTimeout(timeout); reject(new Error(`Members server exited ${code}`)); });
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
    const context = await browser.newContext({ extraHTTPHeaders: { 'X-API-Key': key } });
    const page = await context.newPage();
    const dialogs = [], requests = [];
    page.on('request', request => requests.push({ url: request.url(), method: request.method(), body: request.postData() }));
    page.on('dialog', async dialog => { dialogs.push(dialog.message()); await dialog.accept(); });
    await page.goto(`${base}/#/artifacts`);
    await callback({ page, base, dialogs, requests });
  } finally {
    await browser?.close();
    server.kill();
  }
}


// Polls an async assertion until it holds (the page re-reads the server after each action).
async function until(check, ms = 8000) {
  const end = Date.now() + ms;
  for (;;) {
    try { return await check(); } catch (error) { if (Date.now() > end) throw error; await new Promise(r => setTimeout(r, 50)); }
  }
}
const count = (locator, n) => until(async () => assert.equal(await locator.count(), n));
const hasText = (locator, pattern) => until(async () => assert.match(await locator.first().innerText(), pattern));
const lacksText = (locator, pattern) => until(async () => assert.doesNotMatch(await locator.first().innerText(), pattern));
const enabled = (locator, want) => until(async () => assert.equal(await locator.isEnabled(), want));

const members = page => page.locator('[data-artifact-members]');
const row = (page, name) => page.locator('[data-member-row]', { hasText: name });
const linkRows = page => page.locator('[data-members-links] tbody tr');

test('the Members section lists both members, hostile names stay inert', async () => {
  await withMembers(async ({ page }) => {
    await count(members(page).locator('[data-member-row]'), 2);
    await hasText(row(page, 'Alice'), /2\.0 MiB/);            // deleted, not purged
    await hasText(row(page, 'Alice'), /unlimited/);
    assert.equal(await row(page, 'Alice').locator('progress').count(), 0);
    assert.equal(await page.evaluate(() => window.__x), undefined);
    assert.equal(await members(page).locator('img').count(), 0);
    await hasText(row(page, 'Bob'), /<img src=x onerror=window\.__x=1>/);
  });
});

test('the public-links list shows a member public link and Revoke removes it', async () => {
  await withMembers(async ({ page, requests }) => {
    await count(linkRows(page), 2);                 // default audience filter: public
    await hasText(linkRows(page).first(), /Alice report/);
    await linkRows(page).filter({ hasText: 'Alice report' }).getByRole('button', { name: 'Revoke' }).click();
    await count(linkRows(page), 1);
    assert.ok(requests.some(r => r.method === 'POST' && /\/artifact-members\/member%3Aalice\/shares\/share_alicepub\/revoke$/.test(r.url)));
    await page.locator('[data-members-audience]').selectOption('lab');
    await count(linkRows(page), 1);
    await hasText(linkRows(page).first(), /lab/);
  });
});

test('limits set and clear round-trip through the server', async () => {
  await withMembers(async ({ page }) => {
    await row(page, 'Alice').getByRole('button', { name: 'Alice' }).click();
    const form = page.locator('[data-members-limits]');
    await form.locator('[data-limit="max_member_bytes"]').fill('6');
    await form.locator('[data-limit="max_artifacts"]').fill('8');
    await form.getByRole('button', { name: 'Save limits' }).click();
    await count(row(page, 'Alice').locator('progress'), 2);
    assert.equal(await row(page, 'Alice').locator('progress').first().getAttribute('max'), String(6 * 1048576));
    await page.reload();
    await count(row(page, 'Alice').locator('progress'), 2);
    await row(page, 'Alice').getByRole('button', { name: 'Alice' }).click();
    const again = page.locator('[data-members-limits]');
    assert.equal(await again.locator('[data-limit="max_member_bytes"]').inputValue(), '6');
    await again.locator('[data-limit="max_member_bytes"]').fill('');
    await again.locator('[data-limit="max_artifacts"]').fill('');
    await again.getByRole('button', { name: 'Save limits' }).click();
    await count(row(page, 'Alice').locator('progress'), 0);
    await hasText(row(page, 'Alice'), /unlimited/);
  });
});

test('an invalid limit is refused in the page without a request', async () => {
  await withMembers(async ({ page, requests }) => {
    await row(page, 'Alice').getByRole('button', { name: 'Alice' }).click();
    const form = page.locator('[data-members-limits]');
    await form.locator('[data-limit="max_member_bytes"]').fill('-3');
    await form.getByRole('button', { name: 'Save limits' }).click();
    await hasText(page.locator('[data-members-state]'), /positive/);
    assert.equal(requests.filter(r => r.method === 'POST').length, 0);
  });
});

test('Disable sharing confirms, says live links are revoked, and removes the live link rows', async () => {
  await withMembers(async ({ page, dialogs }) => {
    await row(page, 'Alice').getByRole('button', { name: 'Alice' }).click();
    await page.getByRole('button', { name: 'Disable sharing' }).click();
    await hasText(row(page, 'Alice'), /Sharing disabled/);
    assert.match(dialogs[0], /live link/i);
    assert.match(dialogs[0], /public/i);
    await count(linkRows(page), 1);                 // only Bob's public link remains
    await hasText(linkRows(page).first(), /Bob page/);
    await page.getByRole('button', { name: 'Enable sharing' }).click();
    await lacksText(row(page, 'Alice'), /Sharing disabled/);
  });
});

test('declining the Disable sharing confirmation changes nothing', async () => {
  await withMembers(async ({ page, requests }) => {
    page.removeAllListeners('dialog');
    page.on('dialog', dialog => dialog.dismiss());
    await row(page, 'Alice').getByRole('button', { name: 'Alice' }).click();
    await page.getByRole('button', { name: 'Disable sharing' }).click();
    await page.waitForTimeout(300);
    assert.equal(requests.filter(r => r.method === 'POST').length, 0);
    await count(linkRows(page), 2);
  });
});

test('Purge needs the artifact id typed and then removes the deleted artifact', async () => {
  await withMembers(async ({ page, requests }) => {
    await row(page, 'Alice').getByRole('button', { name: 'Alice' }).click();
    await count(page.getByRole('button', { name: 'Purge', exact: true }), 1);   // only the deleted one
    await page.getByRole('button', { name: 'Purge', exact: true }).click();
    const submit = page.getByRole('button', { name: 'Purge permanently' });
    await enabled(submit, false);
    await page.locator('[data-purge-confirm]').fill('art_alice');
    await enabled(submit, false);
    await page.locator('[data-purge-confirm]').fill('art_alice_gone');
    await enabled(submit, true);
    await submit.click();
    await hasText(page.locator('[data-members-state]'), /purged/);
    await count(page.getByRole('button', { name: 'Purge', exact: true }), 0);
    const post = requests.find(r => r.method === 'POST' && /\/artifacts\/art_alice_gone\/purge$/.test(r.url));
    assert.deepEqual(JSON.parse(post.body), { confirm: 'art_alice_gone' });
  });
});

test('a viewer key sees no Members section', async () => {
  await withMembers(async ({ page, requests }) => {
    await page.locator('[data-artifact-registry]').waitFor();
    await until(() => assert.ok(requests.some(r => /\/api\/v1\/artifact-members$/.test(r.url))));
    await page.waitForTimeout(300);
    assert.equal(await members(page).isHidden(), true);
    assert.equal(await members(page).locator('[data-member-row]').count(), 0);
  }, { key: 'view-key' });
});
