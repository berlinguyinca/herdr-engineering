// The member library feed (Task 10): the same real member_web routes and page as member-artifacts.test.mjs.
// (shared harness copied from it) The member "My artifacts" page against the real member_web routes behind TLS, with a fake Cognito hosted UI and an in-memory
// member service (tests/playwright/member-artifacts-server.py). Real Chromium, real timers, desktop and phone, light and dark.
import { test } from 'node:test';
import assert from 'node:assert';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const REPO = path.resolve(import.meta.dirname, '..', '..');
const PY = process.env.PYTHON || (fs.existsSync(path.join(REPO, '.venv', 'bin', 'python')) ? path.join(REPO, '.venv', 'bin', 'python') : 'python3');
const CHROME = (process.env.HOME+'/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome');

async function startServer(env = {}) {
  const proc = spawn(PY, [path.join(REPO, 'tests', 'playwright', 'member-artifacts-server.py')], {
    env: { ...process.env, ...env }, stdio: ['pipe', 'pipe', 'pipe'],
  });
  const ports = await new Promise((resolve, reject) => {
    let buf = '';
    const timer = setTimeout(() => reject(new Error('server start timeout')), 30000);
    proc.stdout.on('data', (d) => {
      buf += d.toString();
      const a = buf.match(/PORT=(\d+)/); const b = buf.match(/CONTROL=(\d+)/);
      if (a && b) { clearTimeout(timer); resolve({ port: Number(a[1]), control: Number(b[1]) }); }
    });
    proc.on('exit', (c) => reject(new Error(`server exited early (${c})`)));
    proc.stderr.on('data', (d) => { if (!/RuntimeWarning/.test(String(d))) process.stderr.write(`[srv] ${d}`); });
  });
  return { proc, ...ports, base: `https://127.0.0.1:${ports.port}`, ctl: `http://127.0.0.1:${ports.control}` };
}

async function withApp(opts, fn) {
  const { env = {}, viewport = { width: 1280, height: 900 }, colorScheme = 'light' } = opts;
  const srv = await startServer(env);
  let browser;
  try {
    const args = ['--no-sandbox'];
    const launch = fs.existsSync(CHROME) && !process.env.PLAYWRIGHT_NO_FALLBACK ? { headless: true, executablePath: CHROME, args } : { headless: true, args };
    try { browser = await chromium.launch(process.env.PLAYWRIGHT_CHROMIUM ? { headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM, args } : { headless: true, args }); }
    catch { browser = await chromium.launch(launch); }
    const context = await browser.newContext({ viewport, colorScheme, ignoreHTTPSErrors: true });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (e) => errors.push(String(e)));
    return await fn({ page, context, srv, errors });
  } finally {
    if (browser) { try { await browser.close(); } catch { /* noop */ } }
    try { srv.proc.stdin.end(); srv.proc.kill(); } catch { /* noop */ }
  }
}

const post = (url) => fetch(url, { method: 'POST' });
const stats = async (srv) => (await fetch(`${srv.ctl}/stats`)).json();

async function signIn(page, srv) {
  await page.goto(`${srv.base}/`);
  await page.locator('[data-action="signin"]').click();
  await page.locator('[data-feed-row], [data-not-member]').first().waitFor({ timeout: 15000 });
}



const rows = (page) => page.locator('[data-feed-row]');
const titles = async (page) => (await rows(page).locator('.row-title').allInnerTexts());

test('library feed: render, chips, search, expired row, pending, folders, opening content', async () => {
  await withApp({ env: { MEMBER_RESULT_DELAY_MS: '50' } }, async ({ page, context, srv, errors }) => {
    await signIn(page, srv);
    await rows(page).first().waitFor({ timeout: 15000 });

    // header: identity and Sign out
    assert.match(await page.locator('#who').innerText(), /alice/);
    assert.ok(await page.locator('#signout').isVisible());

    // newest first; pending rows are behind their own chip; hostile titles are inert
    assert.equal(await rows(page).count(), 4);
    assert.deepEqual((await titles(page)).map((t) => t.slice(0, 11)), ['QC report "', 'Opened by l', 'Old link', 'Plate map']);
    assert.equal(await page.evaluate(() => window.__x), undefined);
    assert.equal(await page.locator('[data-feed] img').count(), 0);
    for (const [src, text] of [['mine', 'Mine'], ['assigned', 'Assigned'], ['opened', 'Opened via link']]) {
      assert.ok((await page.locator(`[data-feed-row][data-source="${src}"] .badge`).first().innerText()).includes(text));
    }

    // chips
    assert.ok(await page.locator('[data-chip="pending"]').isVisible(), 'Pending chip shows when something is pending');
    await page.locator('[data-chip="assigned"]').click();
    assert.equal(await rows(page).count(), 1);
    await page.locator('[data-chip="opened"]').click();
    assert.equal(await rows(page).count(), 2);
    await page.locator('[data-chip="mine"]').click();
    assert.equal(await rows(page).count(), 1);
    await page.locator('[data-chip="all"]').click();
    assert.equal(await rows(page).count(), 4);

    // search (debounced 150 ms): by title and by sharer
    await page.locator('[data-search]').fill('plate');
    await page.waitForFunction(() => document.querySelectorAll('[data-feed-row]').length === 1);
    assert.deepEqual(await titles(page), ['Plate map']);
    await page.locator('[data-search]').fill('bob@');
    await page.waitForFunction(() => document.querySelectorAll('[data-feed-row]').length === 1);
    await page.locator('[data-search]').fill('nothing-matches');
    await page.locator('[data-empty]').waitFor();
    assert.match(await page.locator('[data-empty]').innerText(), /Nothing here yet/);
    await page.locator('[data-search]').fill('');
    await page.waitForFunction(() => document.querySelectorAll('[data-feed-row]').length === 4);

    // the expired row: muted, tagged, title/sharer/date only, no link and no button
    const expired = page.locator('[data-feed-row="lib_expired"]');
    assert.ok((await expired.getAttribute('class')).includes('row-muted'));
    assert.ok((await expired.innerText()).includes('Expired'));
    assert.equal(await expired.locator('a, button, select').count(), 0);
    assert.equal(await expired.getAttribute('href'), null);

    // opening content: only the server's inline types are shown (plain text, png, pdf); HTML is saved, never rendered
    assert.equal(await page.locator('[data-feed-row="lib_mine"] [data-action="open"]').getAttribute('href'), null, 'no content href to middle-click');
    const [saved] = await Promise.all([page.waitForEvent('download'), page.locator('[data-feed-row="lib_mine"] [data-action="open"]').click()]);
    assert.equal(saved.suggestedFilename(), 'index.html');
    assert.equal(context.pages().length, 1, 'HTML did not open a window');
    const downloads = [];
    page.on('download', (d) => downloads.push(d.suggestedFilename()));
    const [popup] = await Promise.all([context.waitForEvent('page'), page.locator('[data-feed-row="lib_assigned"] [data-action="open"]').click()]);
    await popup.waitForLoadState();
    assert.equal(await popup.evaluate(() => window.opener), null, 'the new window has no opener');
    assert.deepEqual(downloads, [], 'an inline open does not also download the file');
    assert.doesNotMatch(await page.locator('#status, [role="status"], [data-status]').first().innerText().catch(() => ''), /blocked/i);
    assert.match(popup.url(), /^blob:/);
    assert.ok(!popup.url().includes('shr_hidden'));
    assert.equal(await popup.evaluate(() => document.contentType), 'text/plain');
    assert.match(await popup.locator('body').innerText(), /assigned plain text/);
    await popup.close();
    const [img] = await Promise.all([context.waitForEvent('page'), page.locator('[data-feed-row="lib_opened"] [data-action="open"]').click()]);
    await img.waitForLoadState();
    assert.equal(await img.evaluate(() => document.contentType), 'image/png');
    assert.equal(await img.evaluate(() => window.opener), null);
    await img.close();
    assert.deepEqual(downloads, [], 'inline opens never downloaded');

    // Pending: Accept moves a row into the library, Discard removes one
    await page.locator('[data-chip="pending"]').click();
    assert.equal(await rows(page).count(), 2);
    await page.locator('[data-feed-row="lib_pending"] [data-action="accept"]').click();
    await page.locator('[data-feed-row="lib_pending"] [data-action="accept"]').waitFor({ state: 'detached', timeout: 15000 });
    assert.equal(await rows(page).count(), 1);
    // a refused intent (HTTP 200 result "rejected") is an error message with the reason, not a success
    await page.locator('[data-feed-row="lib_pending2"] [data-action="discard"]').click();
    await page.waitForFunction(() => /Refused: not_found/.test(document.querySelector('#status').textContent), null, { timeout: 15000 });
    assert.equal(await page.locator('#status').getAttribute('data-kind'), 'error');
    assert.equal(await page.locator('[data-feed-row="lib_pending2"]').count(), 1, 'the refused row is still there');
    await page.locator('[data-feed-row="lib_pending2"] [data-action="discard"]').click();
    await page.waitForFunction(() => !document.querySelector('[data-chip="pending"]'), null, { timeout: 15000 });
    await page.locator('[data-chip="all"]').click();
    assert.equal(await rows(page).count(), 5);          // the 4 from before plus the accepted one; the discarded one is gone
    assert.equal(await page.locator('[data-feed-row="lib_pending2"]').count(), 0);
    assert.equal(await page.locator('[data-feed-row="lib_pending"] .badge-mine').count(), 1);

    // folders: create, file an item, open the folder view, remove from the folder
    await page.locator('[data-folder-form] input[name="name"]').fill('full');
    await page.locator('[data-folder-form] button[type="submit"]').click();
    await page.waitForFunction(() => /Refused: folder_limit/.test(document.querySelector('#status').textContent), null, { timeout: 15000 });
    assert.equal(await page.locator('.folder-list a[href^="#/f/"]').count(), 0);
    await page.locator('[data-folder-form] input[name="name"]').fill('Topic <b>A</b>');
    await page.locator('[data-folder-form] button[type="submit"]').click();
    const folderLink = page.locator('.folder-list a[href^="#/f/"]');
    await folderLink.waitFor({ timeout: 15000 });
    assert.equal(await page.locator('.folder-list b').count(), 0, 'folder names are escaped');
    await page.locator('[data-feed-row="lib_mine"] select[data-action="file"]').selectOption({ index: 1 });
    await page.waitForFunction(() => document.querySelector('.folder-list a[href^="#/f/"] .row-meta')?.textContent === '1', null, { timeout: 15000 });
    await folderLink.click();
    await page.waitForFunction(() => /^#\/f\//.test(location.hash) && document.querySelectorAll('[data-feed-row]').length === 1);
    assert.deepEqual(await titles(page), ['Plate map']);
    await page.locator('[data-search]').fill('topic');                       // folder names are searched too
    await page.locator('[data-feed-row]').first().waitFor();
    await page.locator('[data-action="unfile"]').click();
    await page.locator('[data-empty]').waitFor({ timeout: 15000 });

    // My artifacts stays reachable
    await page.locator('a.tab', { hasText: 'My artifacts' }).click();
    await page.waitForFunction(() => location.hash === '#/mine');
    await page.locator('[data-empty], [data-artifact-row]').first().waitFor({ timeout: 15000 });

    assert.equal(await page.locator('iframe, frame, object, embed').count(), 0);
    assert.equal(await page.evaluate(() => window.localStorage.length), 0);
    assert.deepEqual(errors, []);
  });
});
