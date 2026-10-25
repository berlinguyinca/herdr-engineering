import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

// Same local withPage as collab-editor.test.mjs until the shared helper (tests/playwright/helpers/fixture.mjs) lands.
async function withPage(run) {
  const root = path.resolve(import.meta.dirname, '../../packages');
  const server = createServer(async (req, res) => {
    if (req.url === '/') { res.setHeader('Content-Type', 'text/html'); res.end('<!doctype html><link rel="stylesheet" href="/herdr-design-system/theme.css"><div id="host"></div>'); return; }
    const file = path.resolve(root, `.${req.url.split('?')[0]}`);
    if (!file.startsWith(root + path.sep)) { res.writeHead(404); res.end(); return; }
    try { res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'application/javascript'); res.end(await readFile(file)); }
    catch { res.writeHead(404); res.end(); }
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  let browser;
  try {
    const fallback = '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
    browser = await chromium.launch({ headless: true, args: ['--no-sandbox'], executablePath: process.env.PLAYWRIGHT_CHROMIUM || (existsSync(fallback) ? fallback : undefined) });
    const page = await browser.newPage({ viewport: { width: 1200, height: 900 } });
    const errors = []; page.on('pageerror', (e) => errors.push(e.message));
    const baseUrl = `http://127.0.0.1:${server.address().port}`;
    await page.goto(`${baseUrl}/`);
    await run(page, baseUrl);
    assert.deepEqual(errors, []);
  } finally { await browser?.close(); await new Promise((r) => server.close(r)); }
}

const by = { type: 'user', id: 'owner', owner_user_id: 'owner' };
const v = (n, size = 10) => ({ version: n, size, sha256: `${n}`.repeat(64).slice(0, 64), by, created_at: '2026-10-24T10:00:00Z' });
const file = (id, name, versions, extra = {}) => ({ id, name, path: `.collab/files/${name}`, version: versions.length, size: versions.at(-1).size,
  sha256: versions.at(-1).sha256, kind: 'file', uploaded_by: by, updated_at: versions.at(-1).created_at, can_delete: true, versions, ...extra });

test('the shelf lists files and versions and patches rows in place', async () => withPage(async (page) => {
  await page.evaluate(async (data) => {
    const { mountFilesShelf } = await import('/collab-files-view.js');
    window.shelf = mountFilesShelf(document.querySelector('#host'), {});
    window.shelf.update(data);
  }, [file('f1', 'plan.md', [v(1)]), file('f2', 'tool.exe', [v(1)], { kind: 'executable' })]);
  const rows = page.locator('[data-file-id]');
  assert.equal(await rows.count(), 2);
  assert.match(await rows.nth(0).innerText(), /plan\.md/);
  assert.match(await rows.nth(0).innerText(), /\.collab\/files\/plan\.md/);
  assert.match(await rows.nth(1).innerText(), /download only/i);
  // mark the row node and open its version list; a new version must update that same node
  await page.evaluate(() => { const r = document.querySelector('[data-file-id="f1"]'); r.__mark = 'same'; r.querySelector('details').open = true; });
  await page.evaluate((data) => window.shelf.update(data), [file('f1', 'plan.md', [v(1), v(2, 20)]), file('f2', 'tool.exe', [v(1)], { kind: 'executable' })]);
  const same = await page.evaluate(() => { const r = document.querySelector('[data-file-id="f1"]'); return { mark: r.__mark, open: r.querySelector('details').open }; });
  assert.deepEqual(same, { mark: 'same', open: true });
  assert.match(await rows.nth(0).locator('[data-version-label]').innerText(), /v2/);
  assert.equal(await rows.nth(0).locator('[data-version]').count(), 2);
  // an unchanged update writes nothing
  const mutations = await page.evaluate((data) => new Promise((resolve) => {
    let n = 0; const mo = new MutationObserver((m) => { n += m.length; });
    mo.observe(document.querySelector('#host'), { subtree: true, childList: true, characterData: true, attributes: true });
    window.shelf.update(data);
    setTimeout(() => { mo.disconnect(); resolve(n); }, 50);
  }), [file('f1', 'plan.md', [v(1), v(2, 20)]), file('f2', 'tool.exe', [v(1)], { kind: 'executable' })]);
  assert.equal(mutations, 0);
  await page.evaluate(() => window.shelf.update([]));
  assert.equal(await rows.count(), 0);
  assert.match(await page.locator('#host').innerText(), /No files yet/);
}));

const PEOPLE = [{ user_id: 'owner', handle: 'owner', online: true }];
async function openRoom(page, agents = []) {
  const posts = [];
  let files = [];
  let stateCount = 0;
  await page.route('**/api/v1/collab/**', async (route) => {
    const request = route.request(), url = new URL(request.url()), where = url.pathname.replace('/api/v1/collab', '');
    if (request.method() === 'POST') {
      const ctype = request.headers()['content-type'] || '';
      if (where === '/rooms/room-one/files') {
        const name = url.searchParams.get('name'), size = request.postDataBuffer().length;
        posts.push({ path: where, name, ctype, size });
        const f = file(`f${files.length + 1}`, name, [v(1, size)]);
        files = [...files, f]; stateCount = 0;
        await route.fulfill({ status: 201, json: { file: { id: f.id, name, version: 1, size, sha256: f.sha256 } } }); return;
      }
      posts.push({ path: where, ctype, body: request.postDataJSON() });
      await route.fulfill({ status: 201, json: { event: {} } }); return;
    }
    let body = {};
    if (where === '/config') body = { client_id: 'test', hosted_ui_domain: 'test', public_url: 'https://example.test', operator: { user_id: 'owner', email: 'owner@example.test' } };
    else if (where === '/rooms') body = { rooms: [{ room_id: 'room-one', name: 'Review', role: 'owner' }] };
    else if (where === '/admin/users') body = { users: [] };
    else if (where === '/rooms/room-one') body = { room_id: 'room-one', name: 'Review', role: 'owner', members: [] };
    else if (where === '/rooms/room-one/state') {
      stateCount++;
      body = { changed: stateCount === 1, seq: files.length + 1, next_cursor: 0, agents, people: PEOPLE, tasks: [], events: [], files };
      if (stateCount > 1) await new Promise((r) => setTimeout(r, 150));
    }
    await route.fulfill({ json: body });
  });
  await page.evaluate(async () => {
    const { mountCollab } = await import('/collab-view.js');
    window.collab = mountCollab(document.querySelector('#host'));
  });
  await page.locator('[data-room="room-one"]').click();
  await page.getByRole('combobox', { name: 'Message' }).waitFor();
  return { posts, kick: () => { stateCount = 0; } };
}

test('a shelf upload is raw bytes, and the @ menu Files group inserts the stable path', async () => withPage(async (page) => {
  const { posts } = await openRoom(page);
  await page.getByRole('tab', { name: 'Files' }).click();
  await page.locator('#hcn-p-files input[type=file]').setInputFiles({ name: 'notes.txt', mimeType: 'text/plain', buffer: Buffer.from('hello') });
  await page.locator('[data-file-id="f1"]').waitFor();
  assert.deepEqual(posts.map((p) => [p.path, p.name, p.ctype, p.size]), [['/rooms/room-one/files', 'notes.txt', 'application/octet-stream', 5]]);
  await page.getByRole('tab', { name: 'Chat' }).click();
  const input = page.getByRole('combobox', { name: 'Message' });
  await input.click();
  await input.pressSequentially('@notes');
  const menu = page.getByRole('listbox', { name: 'References' });
  await menu.getByRole('group', { name: 'Files' }).getByRole('option').first().waitFor();
  await input.press('Enter');
  assert.equal(await input.inputValue(), '.collab/files/notes.txt ');
  await page.evaluate(() => window.collab.dispose());
}));

test('a composer attachment uploads, then the message carries only its id; a literal @file is untouched', async () => withPage(async (page) => {
  const { posts } = await openRoom(page);
  const input = page.getByRole('combobox', { name: 'Message' });
  await page.locator('#hcn-composer input[type=file]').setInputFiles({ name: 'data.csv', mimeType: 'text/csv', buffer: Buffer.from('a,b\n') });
  await page.locator('.conversation-attachment').filter({ hasText: 'Ready' }).waitFor();
  await input.fill('see @file data');
  await input.press('Escape');
  await input.press('Enter');
  await page.waitForFunction(() => !document.querySelector('[aria-label="Message"]').value);
  const msg = posts.find((p) => p.path === '/rooms/room-one/messages');
  assert.equal(msg.body.text, 'see @file data');
  assert.deepEqual(msg.body.files, ['f1']);
  await page.evaluate(() => window.collab.dispose());
}));

test('a task with an attachment is refused before anything is uploaded', async () => withPage(async (page) => {
  const pi = { agent_id: 'pi-id', name: 'lead', address: 'owner.pi', aliases: [], harness_type: 'pi', owner_user_id: 'owner', actions: ['read', 'post', 'execute'], skills: [] };
  const { posts } = await openRoom(page, [pi]);
  const input = page.getByRole('combobox', { name: 'Message' });
  await page.locator('#hcn-composer input[type=file]').setInputFiles({ name: 'plan.md', mimeType: 'text/markdown', buffer: Buffer.from('# plan') });
  await page.locator('.conversation-attachment').filter({ hasText: 'Ready' }).waitFor();
  assert.deepEqual(posts, []);                                   // attaching uploads nothing yet
  await input.fill('@owner.pi do this');
  await input.press('Escape');
  await input.press('Enter');
  await page.locator('.conversation-status').filter({ hasText: 'A task carries text only' }).waitFor();
  assert.deepEqual(posts, []);                                   // refused: no upload, no task
  assert.equal(await input.inputValue(), '@owner.pi do this');
  await page.evaluate(() => window.collab.dispose());
}));
