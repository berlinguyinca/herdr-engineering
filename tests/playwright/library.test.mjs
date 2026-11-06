// The artifact library end to end (Task 13): real Chromium against tests/playwright/library-server.py, which runs a real,
// private PostgreSQL, the worker's intent path, view writer and share-event ingest, the real MemberWeb and the real
// share_public server (both behind TLS), and a fake Cognito hosted UI. Only the object store is in-memory (no RustFS here).
import { test } from 'node:test';
import assert from 'node:assert';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const REPO = path.resolve(import.meta.dirname, '..', '..');
const PY = process.env.PYTHON || (fs.existsSync(path.join(REPO, '.venv', 'bin', 'python')) ? path.join(REPO, '.venv', 'bin', 'python') : 'python3');
const CHROME = '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
const T = 60000;

async function startServer() {
  const proc = spawn(PY, [path.join(REPO, 'tests', 'playwright', 'library-server.py')], { env: process.env, stdio: ['pipe', 'pipe', 'pipe'] });
  const ports = await new Promise((resolve, reject) => {
    let buf = '';
    const timer = setTimeout(() => reject(new Error('server start timeout')), 120000);
    proc.stdout.on('data', (d) => {
      buf += d.toString();
      const a = buf.match(/PORT=(\d+)/); const b = buf.match(/CONTROL=(\d+)/);
      if (a && b) { clearTimeout(timer); resolve({ port: Number(a[1]), control: Number(b[1]) }); }
    });
    proc.on('exit', (c) => reject(new Error(`server exited early (${c})`)));
    proc.stderr.on('data', (d) => { if (!/RuntimeWarning|library content pool/.test(String(d))) process.stderr.write(`[srv] ${d}`); });
  });
  const ctl = `http://127.0.0.1:${ports.control}`;
  const info = await (await fetch(`${ctl}/info`)).json();
  return { proc, ctl, base: `https://127.0.0.1:${ports.port}`, info };
}

async function launch() {
  const args = ['--no-sandbox'];
  try { return await chromium.launch(process.env.PLAYWRIGHT_CHROMIUM ? { headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM, args } : { headless: true, args }); }
  catch { return chromium.launch({ headless: true, executablePath: CHROME, args }); }
}

const post = (url) => fetch(url, { method: 'POST' });
const rows = (page) => page.locator('[data-feed-row]');
const row = (page, id) => page.locator(`[data-feed-row="${id}"]`);

// The worker publishes views on its own cadence, so a fresh view may need a few reloads on a loaded machine.
async function appears(page, selector, seconds = 120) {
  const end = Date.now() + seconds * 1000;
  for (;;) {
    await page.reload();
    try { await page.waitForSelector(selector, { timeout: 8000 }); return; }
    catch (e) { if (Date.now() > end) throw e; }
  }
}

async function signIn(page, base) {
  await page.goto(`${base}/`);
  await page.locator('[data-action="signin"]').click();
  await page.locator('[data-feed-row], [data-empty], [data-not-member]').first().waitFor({ timeout: T });
}

test('library: mine, assigned, filters, folders, pending, public link, claim, revoke', async () => {
  test.setTimeout?.(600000);
  const srv = await startServer();
  const browser = await launch();
  try {
    const { seed } = srv.info;
    await post(`${srv.ctl}/user?name=alice`);
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, ignoreHTTPSErrors: true });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (e) => errors.push(String(e)));
    await signIn(page, srv.base);

    // Mine and Assigned rows, no Opened row yet
    await row(page, seed.mine).waitFor({ timeout: T });
    await row(page, seed.assigned).waitFor({ timeout: T });
    assert.match(await row(page, seed.mine).locator('.badge').first().innerText(), /Mine/);
    assert.match(await row(page, seed.assigned).locator('.badge').first().innerText(), /Assigned/);
    assert.equal(await page.locator('[data-feed-row][data-source="opened"]').count(), 0);

    // chip filter and search
    await page.locator('[data-chip="assigned"]').click();
    assert.deepEqual(await rows(page).locator('.row-title').allInnerTexts(), ['QC report']);
    await page.locator('[data-chip="mine"]').click();
    assert.deepEqual(await rows(page).locator('.row-title').allInnerTexts(), ['Plate map']);
    await page.locator('[data-chip="all"]').click();
    await page.locator('[data-search]').fill('plate');
    await page.waitForFunction(() => document.querySelectorAll('[data-feed-row]').length === 1);
    await page.locator('[data-search]').fill('');
    await page.waitForFunction(() => document.querySelectorAll('[data-feed-row]').length === 2);

    // two folders, one artifact filed into both
    for (const name of ['Plates', 'Review']) {
      await page.locator('[data-folder-form] input[name="name"]').fill(name);
      await page.locator('[data-folder-form] button[type="submit"]').click();
      await page.locator('.folder-list a', { hasText: name }).waitFor({ timeout: T });
    }
    await row(page, seed.mine).locator('select[data-action="file"]').selectOption({ label: 'Plates' });
    await page.waitForFunction((id) => document.querySelector(`[data-feed-row="${id}"] select[data-action="file"]`)?.options.length === 2, seed.mine, { timeout: T });
    await row(page, seed.mine).locator('select[data-action="file"]').selectOption({ label: 'Review' });
    for (const name of ['Plates', 'Review']) {
      await page.locator('.folder-list a', { hasText: name }).click();
      await page.waitForFunction(() => /^#\/f\//.test(location.hash) && document.querySelectorAll('[data-feed-row]').length === 1, null, { timeout: T });
      assert.deepEqual(await rows(page).locator('.row-title').allInnerTexts(), ['Plate map']);
    }
    await page.locator('[data-chip="all"]').click().catch(() => {});
    await page.goto(`${srv.base}/#/library`).catch(() => {});

    // an agent pushes a pending item; accept it
    const pushed = await (await post(`${srv.ctl}/push?title=${encodeURIComponent('Agent <b>push</b>')}`)).json();
    assert.ok(pushed, 'push answered');
    await page.reload();
    await page.locator('[data-chip="pending"]').waitFor({ timeout: T });
    await page.locator('[data-chip="pending"]').click();
    await page.locator('[data-action="accept"]').first().click();
    await page.waitForFunction(() => !document.querySelector('[data-chip="pending"]'), null, { timeout: T })
      .catch(async (e) => { throw new Error(`${e.message}; status=${await page.locator('#status').innerText()}; chips=${await page.locator('[data-chip]').allInnerTexts()}; hash=${await page.evaluate(() => location.hash)}`); });
    await page.locator('[data-chip="mine"]').click();
    await page.waitForFunction(() => document.querySelectorAll('[data-feed-row]').length === 2, null, { timeout: T });
    assert.equal(await page.locator('[data-feed] b').count(), 0, 'hostile titles stay inert');

    // a signed-out visitor opens the public link and sees the sign-in notice
    const guest = await browser.newContext({ viewport: { width: 1000, height: 800 }, ignoreHTTPSErrors: true });
    const gp = await guest.newPage();
    await gp.goto(srv.info.public_url);
    await gp.locator('[data-herdr-notice]').waitFor({ timeout: T });
    assert.match(await gp.locator('[data-herdr-notice]').innerText(), /Sign in with your lab account to add this to your library/);
    // signing in through the link (alice) keeps it in her library
    await gp.locator('[data-herdr-signin]').click();
    await gp.locator('.herdr-signin-button').click();
    await gp.waitForFunction(() => /^\/s\/[A-Za-z0-9_-]{43}\//.test(location.pathname) && !/signin$/.test(location.pathname)
      && !!document.querySelector('[data-herdr-landing]'), null, { timeout: T });
    await gp.locator('[data-herdr-landing]').waitFor({ timeout: T });
    assert.equal(await gp.locator('[data-herdr-notice]').count(), 0, 'signed in: no notice');
    await guest.close();

    await appears(page, `[data-feed-row="${seed.bob}"]`);
    assert.match(await row(page, seed.bob).locator('.badge').first().innerText(), /Opened via link/);
    assert.equal(await row(page, seed.bob).getAttribute('data-status'), null);

    // the sharer revokes: the row is greyed, with no link and no button
    const revoked = await (await post(`${srv.ctl}/revoke`)).json();
    assert.ok(revoked, 'revoke answered');
    await appears(page, `[data-feed-row="${seed.bob}"][data-status="revoked"]`);
    const grey = row(page, seed.bob);
    assert.ok((await grey.getAttribute('class')).includes('row-muted'));
    assert.match(await grey.innerText(), /Revoked/);
    assert.equal(await grey.locator('a, button, select').count(), 0);

    assert.equal(await page.locator('iframe, frame, object, embed').count(), 0);
    assert.deepEqual(errors, []);
    const after = await (await fetch(`${srv.ctl}/info`)).json();
    assert.deepEqual(after.errors, []);
  } finally {
    try { await browser.close(); } catch { /* noop */ }
    try { srv.proc.stdin.end(); srv.proc.kill(); } catch { /* noop */ }
  }
});
