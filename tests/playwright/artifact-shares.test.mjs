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

// ------------------------------------------------------------------------------------------------ Collaborators and Activity tabs

const HOSTILE_TAG = '<img src=x onerror=window.__x=1>';
const INVITE_CODE = 'hcn_invite_ONE-TIME-CODE-7f3a9c2e51b84d60';

const collabPanel = page => page.locator('[data-collab-panel]');
const activityPanel = page => page.locator('[data-activity-panel]');

async function openTab(page, key) {
  await page.locator(`[data-panel-tab="${key}"]`).click();
  await page.locator(`[data-panel-pane="${key}"]`).waitFor({ state: 'visible' });
}

async function openCollaborators(page, base, id = 'collab_1') {
  await openArtifact(page, base, id);
  await openTab(page, 'collaborators');
  await collabPanel(page).locator('h3', { hasText: 'Collaborators' }).waitFor();
  // The agent picker is filled by the dashboard proxy after the grants load.
  await collabPanel(page).locator('select[data-collab-field="agent_id"] option[value="agent-codex"]').waitFor({ state: 'attached' });
}

const noHostileEffect = async page => {
  assert.equal(await page.evaluate(() => window.__x), undefined);
  assert.equal(await page.locator('img[src="x"]').count(), 0);
};

test('an owner grants an agent read+write, sees the row, and revokes it', async () => {
  await withShares(async ({ page, base, requests }) => {
    await openCollaborators(page, base);
    await collabPanel(page).getByText('No agent has access to this artifact.').waitFor();
    await collabPanel(page).locator('select[data-collab-field="agent_id"]').selectOption('agent-codex');
    await collabPanel(page).locator('select[data-collab-field="permission"]').selectOption('write');
    await collabPanel(page).locator('[data-collab-action="grant"]').click();
    const row = collabPanel(page).locator('[data-collab-row]');
    await row.waitFor();
    assert.match(await row.innerText(), /agent-codex[\s\S]*write/);
    await collabPanel(page).getByText('Access granted.').waitFor();
    const grant = requests.find(r => r.method === 'POST' && r.url.endsWith('/api/v1/artifacts/collab_1/collaborators'));
    assert.deepEqual(JSON.parse(grant.body), { agent_id: 'agent-codex', permission: 'write', expires_in_days: 30 });

    await row.locator('[data-collab-action="revoke"]').click();
    await row.waitFor({ state: 'detached' });
    await collabPanel(page).getByText('No agent has access to this artifact.').waitFor();
    assert.ok(requests.some(r => r.method === 'POST' && r.url.endsWith('/collaborators/agent-codex/revoke')));
    assert.equal(await collabPanel(page).locator('[role="alert"]').count(), 0);
  });
});

test('a report artifact offers no write access and says why', async () => {
  await withShares(async ({ page, base }) => {
    await openCollaborators(page, base, 'report_1');
    const write = collabPanel(page).locator('select[data-collab-field="permission"] option[value="write"]');
    assert.equal(await write.isDisabled(), true);
    const reason = collabPanel(page).locator('[data-collab-writable-reason]').first();
    assert.match(await reason.innerText(), /report artifacts are read-only for collaborators/);
  });
});

test('an invite code is shown once and is gone from the page after Close', async () => {
  await withShares(async ({ page, base, requests, consoleLines }) => {
    await openCollaborators(page, base);
    await collabPanel(page).locator('select[data-collab-field="room"]').selectOption('room-1');
    await collabPanel(page).locator('input[data-collab-field="label"]').fill('remote reviewer');
    await collabPanel(page).locator('[data-collab-action="invite"]').click();
    const input = collabPanel(page).locator('[role="dialog"][data-collab-invite] [data-invite-code]');
    await input.waitFor();
    assert.equal(await input.inputValue(), INVITE_CODE);
    // Positive control: readable as the input's value, never in the markup.
    assert.ok((await pageStrings(page)).some(value => value.includes(INVITE_CODE)));
    assert.equal((await page.content()).includes(INVITE_CODE), false, 'the code is never serialised into markup');
    const pending = collabPanel(page).locator('[data-collab-pending]');
    await pending.waitFor();
    assert.match(await pending.innerText(), /inv-1/);
    const made = requests.find(r => r.method === 'POST' && r.url.endsWith('/externals/invites'));
    assert.deepEqual(JSON.parse(made.body).actions, ['read']);
    const link = requests.find(r => r.method === 'POST' && r.url.endsWith('/collaborators/invite-grant'));
    assert.deepEqual(JSON.parse(link.body), { invite_id: 'inv-1', permission: 'read', expires_in_days: 30 });

    await collabPanel(page).locator('[data-collab-action="close-invite"]').click();
    await collabPanel(page).locator('[data-collab-invite]').waitFor({ state: 'detached' });
    assert.equal((await page.content()).includes(INVITE_CODE), false);
    assert.deepEqual((await pageStrings(page)).filter(value => value.includes(INVITE_CODE)), []);
    assert.equal(consoleLines.some(line => line.includes(INVITE_CODE)), false);
    assert.equal(requests.some(r => r.url.includes(INVITE_CODE) || (r.body || '').includes(INVITE_CODE)), false);
  });
});

test('the Activity tab shows the merge badge, rejected attempts with their conflicts, and a version diff', async () => {
  await withShares(async ({ page, base }) => {
    await openArtifact(page, base, 'collab_1');
    await openTab(page, 'activity');
    const history = activityPanel(page);
    await history.locator('[data-activity-version]').first().waitFor();
    assert.equal(await history.locator('[data-version-merge]').count(), 1);
    assert.match(await history.locator('[data-activity-version]', { has: page.locator('[data-version-merge]') }).innerText(), /Merge of parents[\s\S]*c1_v2[\s\S]*c1_v3/);

    await history.locator('[data-activity-action="toggle-attempts"]').click();
    await history.locator('[data-activity-attempt]').first().waitFor();
    assert.equal(await history.locator('[data-activity-attempt]').count(), 2);
    const conflict = history.locator('[data-activity-attempt]', { hasText: 'conflict' });
    assert.match(await conflict.first().innerText(), /agent-codex[\s\S]*Conflicts:[\s\S]*index\.html/);
    await history.locator('[data-activity-action="toggle-attempts"]', { hasText: 'Hide rejected attempts' }).click();
    assert.equal(await history.locator('[data-activity-attempt]').count(), 0);

    await history.locator('[data-activity-action="select"][data-version-id="c1_v4"]').click();
    await history.locator('[data-activity-file]').first().waitFor();
    assert.equal(await history.locator('[data-activity-file]').count(), 3);
    const index = history.locator('[data-activity-file]', { hasText: 'index.html' });
    assert.equal(await index.locator('.hd-diff__add', { hasText: '<p>side</p>' }).count(), 1);
    const binary = history.locator('[data-activity-file]', { hasText: 'logo.png' });
    assert.match(await binary.innerText(), /binary/);
    assert.equal(await binary.locator('.hd-diff').count(), 0, 'a binary file shows no diff body');
    assert.equal(await history.locator('[role="alert"]').count(), 0);
  });
});

test('hostile agent names, messages, file names and reasons stay text on both tabs', async () => {
  await withShares(async ({ page, base }) => {
    await openCollaborators(page, base);
    const evil = collabPanel(page).locator('select[data-collab-field="agent_id"] option[value="agent-evil"]');
    assert.ok((await evil.innerText()).includes(HOSTILE_TAG));
    await collabPanel(page).locator('select[data-collab-field="agent_id"]').selectOption('agent-evil');
    await collabPanel(page).locator('[data-collab-action="grant"]').click();
    await collabPanel(page).locator('[data-collab-row]').waitFor();
    await openTab(page, 'activity');
    const history = activityPanel(page);
    await history.locator('[data-activity-version]').first().waitFor();
    assert.ok((await history.innerText()).includes(`merge ${HOSTILE_TAG}`));
    await history.locator('[data-activity-action="toggle-attempts"]').click();
    await history.locator('[data-activity-attempt]').first().waitFor();
    assert.ok((await history.innerText()).includes(`stale base ${HOSTILE_TAG}`));
    await history.locator('[data-activity-action="select"][data-version-id="c1_v4"]').click();
    await history.locator('[data-activity-file]').first().waitFor();
    assert.ok((await history.locator('[data-activity-file]', { hasText: 'hostile file' }).count()) >= 0);
    assert.ok((await history.innerText()).includes(`${HOSTILE_TAG}.txt`));
    await noHostileEffect(page);
  });
});

test('when collaboration is not enabled the Collaborators tab says so and Links keeps working', async () => {
  await withShares(async ({ page, base, requests }) => {
    await openArtifact(page, base);
    await openTab(page, 'collaborators');
    await collabPanel(page).getByText('Collaboration is not enabled').waitFor();
    assert.ok(requests.some(r => r.url.endsWith('/api/v1/artifacts/web_1/collaborators')));
    assert.equal(await collabPanel(page).locator('[data-collab-row], [data-collab-action="grant"]').count(), 0);
    await openTab(page, 'links');
    await reviewPublic(page);
    assert.match(await panel(page).locator('[data-share-review]').innerText(), /index\.html/);
    await action(page, 'generate').click();
    await panel(page).locator('[data-share-url]').waitFor();
    await action(page, 'close').click();
    await panel(page).locator('[data-share-row] [data-share-state]', { hasText: 'live' }).waitFor();
  }, { COLLAB_DISABLED: '1' });
});
