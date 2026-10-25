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

const MiB = 1024 * 1024;
const settings = (extra = {}) => ({ files_room_bytes: null, files_room_bytes_default: 500 * MiB, files_room_bytes_effective: 500 * MiB,
  files_room_bytes_max: 10240 * MiB, files_used_bytes: 120 * MiB, files_max_bytes: 25 * MiB, can_edit: true, ...extra });

test('the shelf shows used / limit', async () => withPage(async (page) => {
  await page.evaluate(async () => {
    const { mountFilesShelf } = await import('/collab-files-view.js');
    window.shelf = mountFilesShelf(document.querySelector('#host'), {});
    window.shelf.usage({ used_bytes: 120 * 1024 * 1024, limit_bytes: 500 * 1024 * 1024 });
  });
  assert.match(await page.locator('[data-usage]').innerText(), /120\.0 MiB used of 500\.0 MiB/);
  await page.evaluate(() => window.shelf.usage({ used_bytes: 600 * 1024 * 1024, limit_bytes: 500 * 1024 * 1024 }));
  assert.match(await page.locator('[data-usage]').innerText(), /over the limit/i);
}));

test('room properties: the file limit is set in MiB, validated, and saved as bytes', async () => withPage(async (page) => {
  await page.evaluate(async (s) => {
    const { mountRoomProperties } = await import('/collab-files-view.js');
    window.saved = [];
    window.props = mountRoomProperties(document.querySelector('#host'), {
      save: async (body) => { window.saved.push(body); return { ...s, files_room_bytes: body.files_room_bytes, files_room_bytes_effective: body.files_room_bytes ?? s.files_room_bytes_default }; },
    });
    window.props.update(s);
  }, settings());
  const field = page.getByRole('spinbutton', { name: 'File storage limit (MiB)' });
  assert.equal(await field.inputValue(), '');
  assert.match(await page.locator('[data-props-usage]').innerText(), /120\.0 MiB used · limit 500\.0 MiB \(default\) · at most 10240 MiB/);
  const msg = page.locator('[data-props-msg]');
  const save = page.getByRole('button', { name: 'Save room properties' });
  for (const bad of ['0', '-3', '10241', '1.5']) {
    await field.fill(bad);
    await save.click();
    assert.match(await msg.innerText(), /whole number of MiB from 1 to 10240/);
    assert.equal(await field.getAttribute('aria-invalid'), 'true');
  }
  assert.deepEqual(await page.evaluate(() => window.saved), []);          // nothing invalid reaches the server
  await field.fill('2048');
  await save.click();
  await page.waitForFunction(() => window.saved.length === 1);
  assert.deepEqual(await page.evaluate(() => window.saved), [{ files_room_bytes: 2048 * 1024 * 1024 }]);
  assert.match(await msg.innerText(), /Saved/);
  assert.equal(await field.getAttribute('aria-invalid'), 'false');
  // below what the room uses: allowed, with a warning that nothing is deleted
  await field.fill('100');
  await save.click();
  await page.waitForFunction(() => window.saved.length === 2);
  assert.match(await msg.innerText(), /nothing is deleted.*new uploads are blocked/i);
  // empty = back to the default
  await field.fill('');
  await save.click();
  await page.waitForFunction(() => window.saved.length === 3);
  assert.deepEqual((await page.evaluate(() => window.saved))[2], { files_room_bytes: null });
}));

test('a server refusal is shown and the panel is read-only for non-owners', async () => withPage(async (page) => {
  await page.evaluate(async (s) => {
    const { mountRoomProperties } = await import('/collab-files-view.js');
    window.props = mountRoomProperties(document.querySelector('#host'), { save: async () => { throw new Error('only a room owner may change the room\'s properties'); } });
    window.props.update(s);
  }, settings());
  await page.getByRole('spinbutton', { name: 'File storage limit (MiB)' }).fill('600');
  await page.getByRole('button', { name: 'Save room properties' }).click();
  await page.locator('[data-props-msg]').filter({ hasText: 'only a room owner' }).waitFor();
  await page.evaluate((s) => window.props.update(s), settings({ can_edit: false, files_room_bytes: 300 * MiB, files_room_bytes_effective: 300 * MiB }));
  assert.equal(await page.getByRole('spinbutton', { name: 'File storage limit (MiB)' }).isDisabled(), true);
  assert.equal(await page.getByRole('button', { name: 'Save room properties' }).count(), 0);
}));

test('the room owner finds Room properties on the Files tab and saving posts the room settings', async () => withPage(async (page) => {
  const posts = [];
  let current = settings();
  await page.route('**/api/v1/collab/**', async (route) => {
    const request = route.request(), url = new URL(request.url()), where = url.pathname.replace('/api/v1/collab', '');
    if (where === '/rooms/room-one/settings') {
      if (request.method() === 'POST') { const b = request.postDataJSON(); posts.push(b); current = { ...current, files_room_bytes: b.files_room_bytes, files_room_bytes_effective: b.files_room_bytes ?? current.files_room_bytes_default }; }
      await route.fulfill({ json: current }); return;
    }
    let body = {};
    if (where === '/config') body = { client_id: 'test', hosted_ui_domain: 'test', public_url: 'https://example.test', operator: { user_id: 'owner', email: 'owner@example.test' } };
    else if (where === '/rooms') body = { rooms: [{ room_id: 'room-one', name: 'Review', role: 'owner' }] };
    else if (where === '/admin/users') body = { users: [] };
    else if (where === '/rooms/room-one') body = { room_id: 'room-one', name: 'Review', role: 'owner', members: [] };
    else if (where === '/rooms/room-one/state') body = { changed: true, seq: 1, next_cursor: 0, agents: [], people: [], tasks: [], events: [], files: [], files_usage: { used_bytes: 120 * MiB, limit_bytes: current.files_room_bytes_effective } };
    await route.fulfill({ json: body });
  });
  await page.evaluate(async () => {
    const { mountCollab } = await import('/collab-view.js');
    window.collab = mountCollab(document.querySelector('#host'));
  });
  await page.locator('[data-room="room-one"]').click();
  await page.getByRole('tab', { name: 'Files' }).click();
  await page.getByRole('region', { name: 'Room properties' }).waitFor();
  await page.locator('[data-usage]').filter({ hasText: '120.0 MiB used of 500.0 MiB' }).waitFor();
  await page.getByRole('spinbutton', { name: 'File storage limit (MiB)' }).fill('1024');
  await page.getByRole('button', { name: 'Save room properties' }).click();
  await page.locator('[data-props-msg]').filter({ hasText: 'Saved' }).waitFor();
  assert.deepEqual(posts, [{ files_room_bytes: 1024 * MiB }]);
  await page.evaluate(() => window.collab.dispose());
}));
