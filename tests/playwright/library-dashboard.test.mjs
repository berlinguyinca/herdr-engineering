// The dashboard library, read-only, against a REAL artifact host (tests/playwright/library-dashboard-server.py): the
// page renders the host's feed, a row opens #/library/<id>, and the artifact's own page opens inside a fully sandboxed
// iframe (no allow-same-origin, no scripts) served by the dashboard's content proxy.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const ROOT = path.resolve(import.meta.dirname, '../..');
const PYTHON = process.env.PYTHON || (fs.existsSync(path.join(ROOT, '.venv/bin/python')) ? path.join(ROOT, '.venv/bin/python') : 'python3');

async function withLibrary(callback) {
  const server = spawn(PYTHON, [path.join(ROOT, 'tests/playwright/library-dashboard-server.py')], { cwd: ROOT, env: process.env, stdio: ['ignore', 'pipe', 'pipe'] });
  let browser;
  try {
    const started = await new Promise((resolve, reject) => {
      let output = '';
      const timeout = setTimeout(() => reject(new Error('Library server did not start')), 120000);
      server.stdout.on('data', (data) => {
        output += data;
        const port = output.match(/PORT=(\d+)/); const ids = output.match(/IDS=(\{.*\})/);
        if (port && ids) { clearTimeout(timeout); resolve({ port: Number(port[1]), ids: JSON.parse(ids[1]) }); }
      });
      server.stderr.on('data', (data) => { if (!/RuntimeWarning|share-public/.test(String(data))) process.stderr.write(`[library-server] ${data}`); });
      server.once('exit', (code) => { clearTimeout(timeout); reject(new Error(`Library server exited ${code}`)); });
    });
    const options = { headless: true, args: ['--no-sandbox'] };
    if (process.env.PLAYWRIGHT_CHROMIUM) options.executablePath = process.env.PLAYWRIGHT_CHROMIUM;
    browser = await chromium.launch(options);
    const page = await browser.newPage();
    await callback({ page, base: `http://127.0.0.1:${started.port}`, ids: started.ids });
  } finally {
    await browser?.close();
    server.kill('SIGTERM');
  }
}

test('library page renders the host feed with no write control and opens an artifact in a sandboxed iframe', { timeout: 240000 }, async () => {
  await withLibrary(async ({ page, base, ids }) => {
    await page.goto(`${base}/#/library`);
    const rows = page.locator('[data-feed-row]');
    await rows.first().waitFor({ timeout: 30000 });
    assert.equal(await rows.count(), 2);
    await page.getByText('Quarterly findings').first().waitFor();
    for (const control of ['[data-action="accept"]', '[data-action="discard"]', '[data-action="unfile"]', '[data-folder-form]', 'select', '[data-action="rename-folder"]', '[data-action="delete-folder"]']) {
      assert.equal(await page.locator(control).count(), 0, control);
    }

    // a row opens the library item page (never the dashboard's own artifact page)
    await page.locator(`[data-feed-row="${ids.page.artifact}"] a[data-action="open"]`).click();
    await page.waitForURL(new RegExp(`#/library/${ids.page.artifact}$`));
    await page.locator('[data-library-item]').waitFor({ timeout: 30000 });
    assert.equal(await page.locator('[data-library-item] form, [data-library-item] select, [data-library-item] [data-action="revoke"]').count(), 0);
    assert.ok(await page.locator('.library-versions li').count() >= 1);
    assert.ok(await page.locator('.library-shares li').count() >= 1);

    // opening the page puts it into one sandboxed frame
    await page.locator('[data-action="open-file"][data-path="index.html"]').click();
    const frame = page.locator('iframe[data-library-frame]');
    await frame.waitFor();
    assert.equal(await frame.count(), 1);
    assert.equal(await frame.getAttribute('sandbox'), '');
    const src = await frame.getAttribute('src');
    assert.match(src, new RegExp(`^/api/v1/library/content/${ids.page.artifact}/${ids.page.version}/index\\.html$`));
    assert.equal(await frame.getAttribute('srcdoc'), null);

    const inside = page.frameLocator('iframe[data-library-frame]');
    await inside.locator('#t').waitFor({ timeout: 30000 });
    assert.equal(await inside.locator('#t').textContent(), 'static text');      // its script never ran
    await page.waitForTimeout(500);
    assert.equal(await inside.locator('#t').textContent(), 'static text');
    const child = page.frames().find((f) => f.url().includes('/api/v1/library/content/'));
    assert.ok(child);
    assert.equal(await child.evaluate(() => { try { return typeof window.parent.document; } catch { return 'blocked'; } }).catch(() => 'blocked'), 'blocked');

    // the proxied response itself carries the host's sandbox policy and no cookie
    const response = await page.request.get(`${base}${src}`);
    assert.equal(response.status(), 200);
    assert.match(response.headers()['content-security-policy'], /^sandbox/);
    assert.equal(response.headers()['x-content-type-options'], 'nosniff');
    assert.equal(response.headers()['set-cookie'], undefined);
  });
});

test('the library has no write route over HTTP and the feed matches the seeded items', { timeout: 240000 }, async () => {
  await withLibrary(async ({ base, ids }) => {
    for (const method of ['POST', 'PUT', 'PATCH', 'DELETE']) {
      const response = await fetch(`${base}/api/v1/library/folders`, { method, headers: { 'Content-Type': 'application/json' }, body: '{"name":"x"}' });
      assert.ok([403, 405, 501].includes(response.status), `${method} ${response.status}`);
    }
    const feed = await (await fetch(`${base}/api/v1/library`)).json();
    assert.equal(feed.items.length, 2);
    assert.ok(feed.items.some((i) => i.artifact_id === ids.page.artifact));
  });
});
