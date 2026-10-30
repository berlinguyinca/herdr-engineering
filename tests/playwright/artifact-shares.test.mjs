import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const ROOT = path.resolve(import.meta.dirname, '../..');
const PYTHON = process.env.PYTHON || (fs.existsSync(path.join(ROOT, '.venv/bin/python')) ? path.join(ROOT, '.venv/bin/python') : 'python3');
const HOSTILE = '"><img src=x onerror=window.__x=1>';
const HOSTILE_EMAIL = '<img/src=x/onerror=window.__x=1>@x.test';
const HOSTILE_GROUP = '"><script>window.__x=2</script>';
const HEAD_MOVED = 'The head moved — reload and try again';

async function withShares(callback, env = {}) {
  const server = spawn(PYTHON, [path.join(ROOT, 'tests/playwright/artifact-shares-server.py')], {
    cwd: ROOT, env: { ...process.env, ...env }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let browser, output = '';
  try {
    const port = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('Share server did not start')), 20000);
      server.stdout.on('data', data => { output += data; const match = output.match(/PORT=(\d+)/); if (match) { clearTimeout(timeout); resolve(Number(match[1])); } });
      server.stderr.on('data', data => process.stderr.write(`[share-server] ${data}`));
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
    const context = await browser.newContext({ permissions: ['clipboard-read', 'clipboard-write'] });
    await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: base });
    // Every Web Storage write and every cookie set is recorded from the first script on, so "never stored" is checked
    // over the whole session, not only at the end.
    await context.addInitScript(() => {
      window.__storageWrites = [];
      const setItem = Storage.prototype.setItem;
      Storage.prototype.setItem = function (key, value) { window.__storageWrites.push(`${key}=${value}`); return setItem.call(this, key, value); };
      const cookie = Object.getOwnPropertyDescriptor(Document.prototype, 'cookie');
      Object.defineProperty(document, 'cookie', { configurable: true, get() { return cookie.get.call(document); }, set(value) { window.__storageWrites.push(`cookie:${value}`); cookie.set.call(document, value); } });
    });
    const page = await context.newPage();
    const requests = [], consoleLines = [];
    page.on('request', request => requests.push({ url: request.url(), method: request.method(), body: request.postData() }));
    page.on('console', message => consoleLines.push(message.text()));
    page.on('dialog', dialog => dialog.accept());          // Revoke asks confirm()
    const marked = () => (output.match(/^MARK_LIVE /gm) || []).length;
    await callback({ page, base, requests, consoleLines, marked });
  } finally {
    await browser?.close();
    server.kill();
  }
}

const panel = page => page.locator('[data-share-panel]');
const action = (page, name) => panel(page).locator(`[data-share-action="${name}"]`).first();

async function openArtifact(page, base, id = 'web_1') {
  await page.goto(`${base}/#/artifacts/${id}`);
  await panel(page).waitFor();
  await panel(page).locator('[data-share-list-region]').filter({ hasNotText: 'Loading links…' }).waitFor();
}

async function reviewPublic(page, days = '7') {
  await panel(page).locator('[data-share-field="audience"]').selectOption('public');
  await panel(page).locator('[data-share-field="publicDays"]').fill(days);
  await action(page, 'review').click();
  await panel(page).locator('[data-share-review] [data-share-files]').waitFor();
}

// Every string the page holds that a person or script could read back: text, attributes and form values (including
// inside shadow roots), the address, cookies and both Web Storage areas.
async function pageStrings(page) {
  return page.evaluate(() => {
    const out = [location.href, document.cookie, document.title, ...window.__storageWrites];
    for (const area of [localStorage, sessionStorage]) for (let i = 0; i < area.length; i += 1) out.push(area.key(i), area.getItem(area.key(i)));
    const walk = root => {
      for (const node of root.querySelectorAll('*')) {
        out.push(node.textContent || '');
        for (const attribute of node.attributes) out.push(attribute.value);
        if ('value' in node && typeof node.value === 'string') out.push(node.value);
        if (node.shadowRoot) walk(node.shadowRoot);
      }
    };
    walk(document);
    return out;
  });
}

test('a public link is reviewed, generated once, copied, closed without a trace, listed and revoked', async () => {
  await withShares(async ({ page, base, requests, consoleLines }) => {
    await openArtifact(page, base);
    // Private: nothing can be generated.
    assert.equal(await panel(page).locator('[data-share-field="audience"]').inputValue(), 'private');
    await panel(page).locator('[data-share-private]').waitFor();
    assert.equal(await action(page, 'generate').isDisabled(), true);
    assert.equal(await action(page, 'review').isDisabled(), true);
    await panel(page).getByText('No links yet').or(panel(page).locator('herdr-empty')).first().waitFor();

    await reviewPublic(page, '7');
    const summary = await panel(page).locator('[data-share-review]').innerText();
    assert.match(summary, /index\.html/);
    assert.match(summary, /style\.css/);
    assert.match(summary, /2 file\(s\)/);
    assert.match(summary, /Public/);
    const create = requests.find(r => r.method === 'POST' && r.url.endsWith('/api/v1/artifacts/web_1/shares'));
    assert.deepEqual(JSON.parse(create.body), { version_id: 'version_3', audience: 'public', expires_in_days: 7 });
    // The pending share shows in the list before any link exists.
    await panel(page).locator('[data-share-row] [data-share-state]', { hasText: 'pending' }).waitFor();

    await action(page, 'generate').click();
    const urlInput = panel(page).locator('[role="dialog"][data-share-link] [data-share-url]');
    await urlInput.waitFor();
    const url = await urlInput.inputValue();
    assert.ok(url.startsWith(`${base}/s/`), url);
    const token = url.slice(`${base}/s/`.length).replace(/\/$/, '');
    assert.match(token, /^[A-Za-z0-9_-]{40,}$/);
    // Positive control: while the dialog is open the token is readable (as the input's value only).
    assert.ok((await pageStrings(page)).some(value => value.includes(token)));
    assert.equal((await page.content()).includes(token), false, 'the URL is never serialised into markup');

    await action(page, 'copy').click();
    await panel(page).locator('[data-share-copy-status]', { hasText: 'Copied.' }).waitFor();
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), url);

    await action(page, 'close').click();
    await panel(page).locator('[data-share-link]').waitFor({ state: 'detached' });
    assert.equal((await page.content()).includes(token), false);
    assert.deepEqual((await pageStrings(page)).filter(value => value.includes(token)), []);
    assert.equal(requests.some(r => r.url.includes(token)), false);
    assert.equal(consoleLines.some(line => line.includes(token)), false);

    const rows = panel(page).locator('[data-share-row]');
    await panel(page).locator('[data-share-row] [data-share-state]', { hasText: 'live' }).waitFor();
    assert.equal(await rows.count(), 1);
    assert.match(await rows.first().innerText(), /Public · live/);
    assert.match(await rows.first().innerText(), /Anyone with the link/);
    await rows.first().locator('[data-share-counts]', { hasText: '0 opened · 0 denied' }).waitFor();

    await rows.first().locator('[data-share-action="revoke"]').click();
    await rows.first().locator('[data-share-state]', { hasText: 'revoked' }).waitFor();
    assert.equal(await rows.first().locator('[data-share-action="revoke"]').count(), 0);
    const revoke = requests.find(r => r.method === 'POST' && r.url.endsWith('/revoke'));
    assert.equal(revoke.body, '{}');
    assert.equal(await panel(page).locator('[role="alert"]').count(), 0);
    await rows.first().locator('[data-share-action="events"]').click();
    await rows.first().locator('[data-share-events-list]', { hasText: 'revoked' }).waitFor();
    assert.deepEqual((await pageStrings(page)).filter(value => value.includes(token)), []);
  });
});

test('a double click on Generate approves the share once', async () => {
  await withShares(async ({ page, base, requests, marked }) => {
    await openArtifact(page, base);
    await reviewPublic(page);
    // Two synchronous clicks (no actionability wait between them), then a real mouse double click.
    await page.evaluate(() => { const button = document.querySelector('[data-share-action="generate"]'); button.click(); button.click(); });
    await action(page, 'generate').dblclick({ force: true });
    await panel(page).locator('[data-share-url]').waitFor();
    await action(page, 'close').click();
    await panel(page).locator('[data-share-row] [data-share-state]', { hasText: 'live' }).waitFor();
    assert.equal(requests.filter(r => r.method === 'POST' && r.url.endsWith('/approve')).length, 1);
    assert.equal(marked(), 1);
    assert.equal(await panel(page).locator('[data-share-row]').count(), 1);
    assert.equal(await panel(page).locator('[role="alert"]').count(), 0);
  }, { APPROVE_DELAY: '0.5' });
});

test('a pending share left behind is resumed with Review & generate', async () => {
  await withShares(async ({ page, base, requests }) => {
    await openArtifact(page, base);
    await reviewPublic(page);
    await page.goto(`${base}/#/artifacts`);
    await page.locator('[data-artifact-registry]').waitFor();
    await page.goto(`${base}/#/artifacts/web_1`);
    await panel(page).locator('[data-share-row] [data-share-state]', { hasText: 'pending' }).waitFor();
    assert.equal(await panel(page).locator('[data-share-review]').count(), 0);
    assert.equal(await action(page, 'generate').isDisabled(), true);
    await panel(page).locator('[data-share-row] [data-share-action="continue"]').click();
    await panel(page).locator('[data-share-review] [data-share-files]').waitFor();
    assert.match(await panel(page).locator('[data-share-review]').innerText(), /index\.html/);
    await action(page, 'generate').click();
    const url = await panel(page).locator('[data-share-url]').inputValue();
    assert.ok(url.startsWith(`${base}/s/`));
    await action(page, 'close').click();
    await panel(page).locator('[data-share-row] [data-share-state]', { hasText: 'live' }).waitFor();
    assert.equal(requests.filter(r => r.method === 'POST' && r.url.endsWith('/shares')).length, 1);
    assert.equal(await panel(page).locator('[data-share-row]').count(), 1);
  });
});

test('hostile titles, emails and groups stay text', async () => {
  await withShares(async ({ page, base }) => {
    await openArtifact(page, base);
    await page.getByRole('heading', { name: `Lab notes ${HOSTILE}` }).waitFor();
    await panel(page).locator('[data-share-field="audience"]').selectOption('lab');
    await panel(page).locator('[data-share-field="emails"]').fill(HOSTILE_EMAIL);
    await panel(page).locator('[data-share-field="groups"]').fill(HOSTILE_GROUP);
    await panel(page).locator('[data-share-field="title"]').fill(HOSTILE);
    await action(page, 'review').click();
    await panel(page).locator('[data-share-review] [data-share-files]').waitFor();
    assert.ok((await panel(page).locator('[data-share-review]').innerText()).includes(HOSTILE_EMAIL.toLowerCase()));
    await action(page, 'generate').click();
    await panel(page).locator('[data-share-url]').waitFor();
    await action(page, 'close').click();
    const row = panel(page).locator('[data-share-row]').first();
    await row.locator('[data-share-state]', { hasText: 'live' }).waitFor();
    const text = await row.innerText();
    assert.ok(text.includes(HOSTILE), text);
    assert.ok(text.includes(HOSTILE_EMAIL.toLowerCase()), text);
    assert.ok(text.includes(HOSTILE_GROUP), text);
    assert.equal(await page.evaluate(() => window.__x), undefined);
    assert.equal(await page.locator('img[src="x"], script:not([src])').evaluateAll(nodes => nodes.filter(node => /__x/.test(node.outerHTML) || node.tagName === 'IMG').length), 0);
  });
});

test('restoring an older version makes a new head that the history and Share form show', async () => {
  await withShares(async ({ page, base, requests }) => {
    await openArtifact(page, base);
    const history = page.locator('[data-artifact-versions]');
    await history.locator('[data-version-row]').first().waitFor();
    assert.equal(await history.locator('[data-version-row][data-version-id="version_3"] [data-history-action="restore"]').count(), 0);
    assert.match(await history.locator('[data-version-row][data-version-id="version_2"]').innerText(), /api-key:author2:local[\s\S]*Parent: version_1/);
    await history.locator('[data-version-row][data-version-id="version_1"] [data-history-action="restore"]').click();
    await history.locator('[data-history-status]', { hasText: 'Restored as v4' }).waitFor();
    const head = history.locator('[data-version-row][data-version-id="version_4"]');
    await head.locator('[data-version-head]').waitFor();
    assert.match(await head.innerText(), /Parent: version_3/);
    assert.equal(await history.locator('[data-version-head]').count(), 1);
    const commit = requests.find(r => r.method === 'POST' && r.url.endsWith('/api/v1/artifacts/web_1/versions'));
    const body = JSON.parse(commit.body);
    assert.equal(body.base_version_id, 'version_3');
    assert.deepEqual(body.provenance, { restored_from: 'version_1' });
    assert.equal(body.idempotency_key, 'restore-version_1-version_3');
    // The Share form follows the new head.
    assert.equal(await panel(page).locator('[data-share-field="versionId"]').inputValue(), 'version_3');
    assert.ok((await panel(page).locator('[data-share-field="versionId"] option[value="version_4"]').innerText()).includes('(current)'));
  });
});

test('a restore against a moved head says so', async () => {
  await withShares(async ({ page, base }) => {
    await openArtifact(page, base);
    const history = page.locator('[data-artifact-versions]');
    await history.locator('[data-version-row][data-version-id="version_1"] [data-history-action="restore"]').click();
    await history.locator('[data-history-status] [role="alert"]', { hasText: HEAD_MOVED }).waitFor();
    assert.equal(await history.locator('[data-version-row]').count(), 3);
  }, { RESTORE_CONFLICT: '1' });
});

test('a report artifact page shows the Share card and shares its rendered preview', async () => {
  await withShares(async ({ page, base }) => {
    await openArtifact(page, base, 'report_1');
    await page.getByText('Finding one.', { exact: true }).waitFor();
    assert.equal(await page.locator('[data-history-action="restore"]').count(), 0);
    assert.equal(await panel(page).locator('[data-share-field="entry"]').count(), 0);
    await reviewPublic(page);
    assert.match(await panel(page).locator('[data-share-review]').innerText(), /preview\.html/);
    await action(page, 'generate').click();
    assert.ok((await panel(page).locator('[data-share-url]').inputValue()).startsWith(`${base}/s/`));
    await action(page, 'close').click();
    await panel(page).locator('[data-share-row] [data-share-state]', { hasText: 'live' }).waitFor();
  });
});
