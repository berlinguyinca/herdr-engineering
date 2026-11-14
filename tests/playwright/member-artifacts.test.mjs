// The member "My artifacts" page against the real member_web routes behind TLS, with a fake Cognito hosted UI and an in-memory
// member service (tests/playwright/member-artifacts-server.py). Real Chromium, real timers, desktop and phone, light and dark.
import { test } from 'node:test';
import assert from 'node:assert';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { chromium } from 'playwright';

const REPO = path.resolve(import.meta.dirname, '..', '..');
const PY = process.env.PYTHON || (fs.existsSync(path.join(REPO, '.venv', 'bin', 'python')) ? path.join(REPO, '.venv', 'bin', 'python') : 'python3');
const CHROME = '/home/user/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';

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
  // the library feed is the landing page now; these tests exercise the owner page, which lives at #/mine
  await page.locator('[data-feed-row], [data-empty], [data-not-member]').first().waitFor({ timeout: 15000 });
  if (await page.locator('[data-not-member]').count()) return;
  await page.evaluate(() => { location.hash = '#/mine'; });
  await page.locator('[data-empty], [data-artifact-row]').first().waitFor({ timeout: 15000 });
}

function bytes(n) { return crypto.randomBytes(n); }

async function createArtifact(page, title) {
  await page.goto(page.url().replace(/#.*$/, '') + '#/new');
  await page.locator('[data-new-form] input[name="title"]').fill(title);
  await page.locator('[data-new-form] button[type="submit"]').click();
  await page.locator('[data-artifact-header]').waitFor({ timeout: 15000 });
}

// Records every phase an upload row passes through so short-lived "Checking file" states are not missed by polling.
const OBSERVER = () => {
  window.__phases = [];
  window.__csp = [];
  document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
  const note = () => document.querySelectorAll('[data-upload]').forEach((el) => {
    const key = `${el.getAttribute('data-upload')}:${el.getAttribute('data-phase')}`;
    const bar = el.querySelector('progress');
    if (!window.__phases.includes(key)) window.__phases.push(key);
    if (bar && Number(bar.value) > 0 && !window.__phases.includes(`${key}:progress`)) window.__phases.push(`${key}:progress`);
  });
  new MutationObserver(note).observe(document, { subtree: true, childList: true, attributes: true });
};

const COMBOS = [
  ['desktop light', { width: 1280, height: 900 }, 'light'],
  ['desktop dark', { width: 1280, height: 900 }, 'dark'],
  ['mobile light', { width: 390, height: 800 }, 'light'],
  ['mobile dark', { width: 390, height: 800 }, 'dark'],
];

for (const [label, viewport, colorScheme] of COMBOS) {
  test(`full member flow, ${label}`, async () => {
    await withApp({ viewport, colorScheme, env: { MEMBER_RUN_DELAY_MS: '40' } }, async ({ page, srv, errors }) => {
      await page.addInitScript(OBSERVER);
      await signIn(page, srv);
      assert.ok(await page.locator('[data-empty]').first().isVisible(), 'the list starts empty');
      assert.equal(await page.evaluate(() => window.localStorage.length), 0, 'nothing is kept in localStorage');

      await createArtifact(page, 'Lab notes');
      const big = bytes(3 * 1024 * 1024 + 123);
      await page.locator('[data-file-input]').setInputFiles([
        { name: 'index.html', mimeType: 'text/html', buffer: big },
        { name: 'style.css', mimeType: 'text/css', buffer: Buffer.from('body{}') },
      ]);
      await page.locator('[data-upload="index.html"][data-phase="done"]').waitFor({ timeout: 30000 });
      await page.locator('[data-upload="style.css"][data-phase="done"]').waitFor({ timeout: 30000 });
      const phases = await page.evaluate(() => window.__phases);
      assert.ok(phases.includes('index.html:hashing'), `hashing was shown (${phases})`);
      assert.ok(phases.includes('index.html:uploading:progress'), `upload progress was shown (${phases})`);
      const s = await stats(srv);
      assert.ok(s.run_puts >= 4, `a multi-run upload (${s.run_puts})`);
      assert.equal(s.duplicate_runs, 0);

      await page.locator('[data-publish-message]').fill('first');
      await page.locator('[data-action="publish"]').click();
      await page.locator('[data-history]').getByText('v1').first().waitFor({ timeout: 15000 });

      // public share
      await page.locator('[data-share-form] input[value="public"]').check();
      assert.equal(await page.locator('[data-public-text]').innerText(), "Anyone with the link can view this version. It shows 'Shared by alice'.");
      assert.equal(await page.locator('input[name="public_days"]').inputValue(), '30');
      await page.locator('[data-share-form] button[type="submit"]').click();
      const summary = page.locator('[data-share-summary]');
      await summary.waitFor({ timeout: 15000 });
      assert.match(await summary.locator('[data-summary-audience]').innerText(), /public/i);
      assert.ok((await summary.locator('[data-summary-expiry]').innerText()).length > 4, 'the expiry is shown');
      assert.equal(await summary.locator('[data-summary-files] li').count(), 2);
      await summary.locator('[data-action="approve"]').click();
      const url = await page.locator('[data-secret-url]').inputValue();
      assert.match(url, /^https:\/\/127\.0\.0\.1:\d+\/s\/[\w-]+\/$/);
      assert.ok(await page.locator('[data-shown-once]').isVisible());

      // shown once: the URL is gone after navigating away and back
      await page.goto(page.url().replace(/#.*$/, '') + '#/links');
      await page.locator('[data-link-row]').first().waitFor();
      assert.equal(await page.locator('[data-secret-url]').count(), 0);
      assert.match(await page.locator('[data-link-row]').first().innerText(), /Public/);
      assert.ok(!(await page.content()).includes(url), 'the link is not kept anywhere in the page');
      await page.locator('[data-link-row] [data-action="revoke"]').first().click();
      await page.locator('[data-link-row] [data-action="revoke"]').waitFor({ state: 'detached', timeout: 15000 });

      // lab share, then delete
      await page.goBack();
      await page.locator('[data-share-form]').waitFor();
      await page.locator('[data-share-form] input[name="emails"]').fill('bob@lab.example');
      await page.locator('[data-share-form] button[type="submit"]').click();
      await page.locator('[data-share-summary]').waitFor({ timeout: 15000 });
      assert.match(await page.locator('[data-summary-audience]').innerText(), /lab/i);
      await page.locator('[data-action="approve"]').click();
      await page.locator('[data-secret-url]').waitFor({ timeout: 15000 });

      await page.locator('[data-action="delete"]').click();
      const dialog = page.locator('[data-delete-dialog]');
      assert.match(await dialog.innerText(), /Lab notes/);
      assert.match(await dialog.innerText(), /Its links stop working\. Only the operator can recover or purge it\./);
      await dialog.locator('[data-action="confirm-delete"]').click();
      await page.locator('[data-delete-dialog]').waitFor({ state: 'detached', timeout: 15000 });
      await page.goto(page.url().replace(/#.*$/, '') + '#/mine');
      await page.locator('[data-empty]').first().waitFor({ timeout: 15000 });
      assert.equal(await page.locator('[data-artifact-row]').count(), 0, 'the deleted artifact is no longer listed');
      await page.goto(page.url().replace(/#.*$/, '') + '#/links');
      await page.getByRole('heading', { name: 'My links' }).waitFor({ timeout: 15000 });
      await page.locator('[data-empty]').first().waitFor({ timeout: 15000 });
      assert.equal(await page.locator('[data-link-row]').count(), 0);

      assert.equal(await page.locator('frame, iframe, object, embed, frameset').count(), 0, 'no frame, object or embed element');
      assert.equal(await page.evaluate(() => window.localStorage.length), 0);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), true, 'no horizontal scroll');
      assert.deepEqual(errors, []);
      assert.deepEqual(await page.evaluate(() => window.__csp), [], 'no CSP violation anywhere in the flow');
    });
  });
}

test('a user outside the lab group is told so', async () => {
  await withApp({}, async ({ page, srv }) => {
    await post(`${srv.ctl}/user?name=mal`);
    await signIn(page, srv);
    assert.match(await page.locator('[data-not-member]').innerText(), /Your account is not a member of the lab group/);
  });
});

test('an upload interrupted by a reload resumes without resending stored runs', async () => {
  await withApp({ env: { MEMBER_RUN_DELAY_MS: '250' } }, async ({ page, srv }) => {
    await signIn(page, srv);
    await createArtifact(page, 'Resume me');
    const file = { name: 'big.bin', mimeType: 'application/octet-stream', buffer: bytes(8 * 1024 * 1024) };
    await page.locator('[data-file-input]').setInputFiles([file]);
    await page.locator('[data-upload="big.bin"][data-phase="uploading"]').waitFor({ timeout: 20000 });
    for (let i = 0; i < 100 && (await stats(srv)).run_puts < 3; i += 1) await page.waitForTimeout(100);
    assert.ok((await stats(srv)).run_puts >= 3, 'some runs were stored before the reload');
    await page.reload();
    await page.locator('[data-upload="big.bin"][data-phase="resume"]').waitFor({ timeout: 15000 });
    const before = (await stats(srv)).run_puts;
    await page.locator('[data-file-input]').setInputFiles([file]);
    await page.locator('[data-upload="big.bin"][data-phase="done"]').waitFor({ timeout: 40000 });
    const after = await stats(srv);
    assert.equal(after.duplicate_runs, 0, 'no stored run was sent again');
    assert.ok(after.run_puts - before < 8 - 2, `only the missing runs were sent (${after.run_puts - before})`);
    assert.ok(after.max_in_flight <= 3);
  });
});

test('an expired member session is re-exchanged once and the request retried', async () => {
  await withApp({}, async ({ page, srv }) => {
    await signIn(page, srv);
    await post(`${srv.ctl}/rotate`);
    const exchanges = [];
    page.on('request', (r) => { if (r.url().endsWith('/api/member/session')) exchanges.push(r.url()); });
    await createArtifact(page, 'After rotation');
    assert.equal(exchanges.length, 1, 'one re-exchange');
    assert.ok(await page.locator('[data-artifact-header]').isVisible());
  });
});

test('hostile titles are shown as text', async () => {
  await withApp({}, async ({ page, srv, errors }) => {
    await signIn(page, srv);
    await page.goto(`${srv.base}/my/#/shared`.replace('/my/', '/'));
    await page.locator('[data-shared-list] a').first().waitFor({ timeout: 15000 });
    assert.ok((await page.locator('[data-shared-list]').innerText()).includes('onerror=window.__x=1'));
    assert.equal(await page.evaluate(() => window.__x), undefined);
    assert.equal(await page.locator('[data-shared-list] img').count(), 0);
    assert.deepEqual(errors, []);
  });
});

// A published artifact with a head version to share.
async function publishedArtifact(page, srv, title) {
  await signIn(page, srv);
  await createArtifact(page, title);
  await page.locator('[data-file-input]').setInputFiles([{ name: 'index.html', mimeType: 'text/html', buffer: Buffer.from('<p>hi</p>') }]);
  await page.locator('[data-upload="index.html"][data-phase="done"]').waitFor({ timeout: 15000 });
  await page.locator('[data-action="publish"]').click();
  await page.locator('[data-history]').getByText('v1').first().waitFor({ timeout: 15000 });
}

async function reviewPublicShare(page) {
  await page.locator('[data-share-form] input[value="public"]').check();
  await page.locator('[data-share-form] button[type="submit"]').click();
  await page.locator('[data-share-summary]').waitFor({ timeout: 15000 });
}

test('a double-clicked Approve sends one approval and the one-time URL stays on screen', async () => {
  await withApp({}, async ({ page, srv }) => {
    await publishedArtifact(page, srv, 'Double click');
    await reviewPublicShare(page);
    const approve = page.locator('[data-action="approve"]');
    await approve.dblclick();
    assert.equal(await page.locator('[data-action="cancel-share"]').isDisabled().catch(() => true), true, 'Cancel is off while approving');
    const url = await page.locator('[data-secret-url]').inputValue({ timeout: 15000 });
    await page.waitForLoadState('networkidle');
    assert.equal(await page.locator('[data-secret-url]').inputValue(), url, 'the URL was not replaced');
    const s = await stats(srv);
    assert.equal(s.approve_calls, 1);
    assert.equal(s.revokes, 0, 'nothing was revoked');
  });
});

for (const [reason, pattern] of [['not_pending', /no longer waiting/], ['expired', /expired/], ['not_found', /no longer exists/], ['secret_unavailable', /revoked/]]) {
  test(`an approval refused as ${reason} is reported as a failure, never as a live link`, async () => {
    await withApp({}, async ({ page, srv }) => {
      await publishedArtifact(page, srv, `Refused ${reason}`);
      await reviewPublicShare(page);
      await post(`${srv.ctl}/fault?approve=${reason}`);
      await page.locator('[data-action="approve"]').click();
      await page.locator('[data-share-region] [data-error]').waitFor({ timeout: 15000 });
      const text = await page.locator('[data-share-region]').innerText();
      assert.match(text, pattern);
      assert.doesNotMatch(text, /is live/);
      assert.equal(await page.locator('[data-secret-url]').count(), 0);
    });
  });
}

test('a failed approval that leaves the link pending can be retried and then shows the URL', async () => {
  await withApp({}, async ({ page, srv }) => {
    await publishedArtifact(page, srv, 'Retry approval');
    await reviewPublicShare(page);
    await post(`${srv.ctl}/fault?approve=approval_failed`);
    await page.locator('[data-action="approve"]').click();
    await page.locator('[data-share-summary] [data-form-error]:not([hidden])').waitFor({ timeout: 15000 });
    assert.equal(await page.locator('[data-action="approve"]').isEnabled(), true, 'Approve is available again');
    await page.locator('[data-action="approve"]').click();
    assert.match(await page.locator('[data-secret-url]').inputValue({ timeout: 15000 }), /\/s\//);
    const s = await stats(srv);
    assert.equal(s.approve_calls, 2);
    assert.notEqual(s.approve_keys[0], s.approve_keys[1], 'a new attempt uses a new key');
  });
});

test('leaving a share that was never approved revokes it', async () => {
  await withApp({}, async ({ page, srv }) => {
    await publishedArtifact(page, srv, 'Abandoned');
    await reviewPublicShare(page);
    await page.goto(page.url().replace(/#.*$/, '') + '#/links');
    await page.getByRole('heading', { name: 'My links' }).waitFor();
    for (let i = 0; i < 50 && (await stats(srv)).revokes < 1; i += 1) await page.waitForTimeout(100);
    assert.equal((await stats(srv)).revokes, 1);
  });
});

test('a malformed address in the hash shows a not-found view without an error', async () => {
  await withApp({}, async ({ page, srv, errors }) => {
    await signIn(page, srv);
    await page.goto(page.url().replace(/#.*$/, '') + '#/a/%E0');
    await page.locator('[data-empty]').getByText('not available').waitFor({ timeout: 10000 });
    assert.deepEqual(errors, []);
  });
});
