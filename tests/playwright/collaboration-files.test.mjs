import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { chromium } from 'playwright';

const manifest = { files: [{ relative_path: 'new.csv', size_bytes: 3, sha256: 'a'.repeat(64), parts: [{ size_bytes: 3, sha256: 'a'.repeat(64) }] }] };
// The real shared view/client runs in Chromium. HTTP fixtures document the
// not-yet-wired file/access/upload contracts; they are not live-backend proof.
async function withFiles(surface, fn, { role = 'owner', canUpload = true, statusResponse } = {}) {
  const server = createServer(async (req, res) => {
    if (req.url === '/') { res.setHeader('Content-Type', 'text/html'); res.end('<link rel="stylesheet" href="/collab.css"><div id="view"></div>'); return; }
    try { res.setHeader('Content-Type', req.url.endsWith('.css') ? 'text/css' : 'application/javascript'); res.end(await readFile(new URL(`../../packages/${req.url.slice(1)}`, import.meta.url))); }
    catch { res.writeHead(404); res.end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  const calls = [], holds = new Map();
  if (statusResponse) holds.set('/uploads/agent-u1', statusResponse);
  const room = { room_id: 'r1', name: 'Lab', role, file_upload_permission: canUpload, members: [
    { user_id: 'owner', email: 'owner@example.org', role: 'owner', file_upload_permission: true },
    { user_id: 'member', email: 'member@example.org', role: 'viewer', file_upload_permission: false },
  ] };
  const files = [
    { file_id: 'a', room_id: 'r1', relative_path: 'data/a.csv', status: 'available', size_bytes: 3, preview_status: 'pending' },
    { file_id: 'b', room_id: 'r1', relative_path: 'data/deep/b.json', status: 'available', size_bytes: 9, preview_status: 'failed' },
    { file_id: 'unfinished', room_id: 'r1', relative_path: 'unfinished.csv', status: 'verifying' },
  ];
  const batch = { upload_id: 'agent-u1', room_id: 'r1', status: 'uploading', file_count: 10000, total_bytes: 100, uploaded_bytes: 50, completed_parts: 1, total_parts: 2 };
  try {
    const fallback = '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
    browser = await chromium.launch({ headless: true, args: ['--no-sandbox'], executablePath: process.env.PLAYWRIGHT_CHROMIUM || (existsSync(fallback) ? fallback : undefined) });
    const page = await browser.newPage(); page.setDefaultTimeout(2000);
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    const origin = `http://127.0.0.1:${server.address().port}`;
    await page.route('**/api/v1/collab/**', async route => {
      const req = route.request(), url = new URL(req.url()), path = url.pathname.replace('/api/v1/collab', '');
      const body = req.postDataJSON(); calls.push({ path, body, method: req.method(), query: url.search });
      let result;
      if (holds.has(path)) result = await holds.get(path);
      else if (path === '/config') result = { client_id: 'web', hosted_ui_domain: origin, public_url: origin };
      else if (path === '/me') result = { user_id: role === 'owner' ? 'owner' : 'member', status: 'approved' };
      else if (path === '/rooms') result = { rooms: [room] };
      else if (path === '/rooms/r1') result = room;
      else if (path === '/rooms/r1/state') { await new Promise(r => setTimeout(r, 80)); result = { changed: false, seq: 1 }; }
      else if (path === '/rooms/r1/files') result = { files: url.search ? [{ file_id: 'c', room_id: 'r1', relative_path: 'last.txt', status: 'available' }] : files, next_cursor: url.search ? null : 'page/2', has_more: !url.search, uploads: [batch] };
      else if (path.endsWith('/access')) result = { url: 'https://objects.example/authorized', expires_at: Date.now() / 1000 + 60 };
      else if (path === '/rooms/r1/uploads/preview') result = { room_id: 'r1', file_count: 1, total_bytes: 3, can_upload: canUpload, access_request_required: !canUpload };
      else if (path === '/rooms/r1/uploads') result = { http: 403, error: 'A registered agent must create its own upload' };
      else if (path === '/uploads/agent-u1') result = batch;
      else if (path === '/rooms/r1/members/member/upload-permission') { room.members[1].file_upload_permission = body.file_upload_permission; result = {}; }
      else result = {};
      await route.fulfill({ status: result.http || 200, contentType: 'application/json', body: JSON.stringify(result) }).catch(() => {});
    });
    await page.route('https://objects.example/**', route => route.fulfill({ contentType: 'application/octet-stream', headers: { 'content-disposition': 'attachment; filename="a.csv"' }, body: 'abc' }));
    await page.goto(origin);
    await page.evaluate(async surface => {
      sessionStorage.setItem('hcn.tokens', JSON.stringify({ access: `h.${btoa(JSON.stringify({ exp: Date.now() / 1000 + 600 }))}.s`, id: 'id' }));
      const { mountCollab } = await import('/collab-view.js');
      window.view = mountCollab(document.querySelector('#view'), { surface }); await window.view.ready;
    }, surface);
    await page.locator('[data-room="r1"]').click();
    await page.getByRole('tab', { name: 'Files', exact: true }).click();
    await fn({ page, calls, holds, room, files, batch });
    assert.deepEqual(errors, []);
  } finally { holds.clear(); await browser?.close(); await new Promise(resolve => server.close(resolve)); }
}

for (const surface of ['public', 'dashboard']) {
  test(`files ${surface}: navigation, search, pagination and unchanged poll preserve rows`, async () => withFiles(surface, async ({ page, files }) => {
    files[0].relative_path = 'data/a&\'quoted.csv';
    // The initial file request may have captured the old name before this
    // fixture mutation. Establish the new snapshot before checking row reuse.
    await page.getByRole('button', { name: 'Refresh files', exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('[data-refresh-files]').disabled);
    await page.locator('[data-file-path="data"]').click();
    await page.locator('[data-file-id="a"]').waitFor();
    assert.equal(await page.getByText('unfinished.csv').count(), 0);
    await page.locator('[data-file-id="a"]').evaluate(el => { window.originalRow = el; });
    await page.getByRole('button', { name: 'Refresh files', exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('[data-refresh-files]').disabled);
    assert.equal(await page.locator('[data-file-id="a"]').evaluate(el => el === window.originalRow), true);
    await page.getByLabel('Search loaded files').fill('B.JSON');
    await page.getByText('data/deep/b.json', { exact: true }).waitFor();
    await page.getByLabel('Search loaded files').fill('');
    await page.getByRole('button', { name: 'All files', exact: true }).click();
    await page.getByRole('button', { name: 'Load more files' }).click();
    await page.getByText('last.txt', { exact: true }).waitFor();
    await page.getByRole('button', { name: 'Refresh files', exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('[data-refresh-files]').disabled);
    assert.equal(await page.getByText('last.txt', { exact: true }).count(), 1);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  }));

  test(`files ${surface}: download and preview require fresh authorization, denial remains visible`, async () => withFiles(surface, async ({ page, calls, holds }) => {
    await page.locator('[data-file-path="data"]').click();
    const row = page.locator('[data-file-id="a"]');
    const download = page.waitForEvent('download');
    await row.getByRole('button', { name: 'Download', exact: true }).click();
    assert.equal((await download).suggestedFilename(), 'a.csv');
    await row.getByRole('button', { name: 'Preview', exact: true }).click();
    await page.getByText(/Preview authorized/).waitFor();
    assert.deepEqual(calls.filter(c => c.path.endsWith('/access')).map(c => c.body.purpose), ['download', 'preview']);
    assert.equal(await page.locator('iframe, object, embed').count(), 0);
    holds.set('/rooms/r1/files/a/access', Promise.resolve({ http: 403, error: 'Room access revoked' }));
    await row.getByRole('button', { name: 'Download', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: 'Room access revoked' }).waitFor();
  }));

  test(`files ${surface}: browser previews and hands off the manifest without creating a user session`, async () => withFiles(surface, async ({ page, calls, files, batch }) => {
    await page.getByText(/10000 files/).waitFor();
    assert.equal(await page.locator('#hcn-p-files progress').count(), 1);
    await page.getByLabel('Upload manifest').fill(JSON.stringify(manifest));
    await page.getByRole('button', { name: 'Preview upload', exact: true }).click();
    await page.getByText('1 files · 3 bytes', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Start upload', exact: true }).count(), 0);
    const handoff = page.getByLabel('Agent upload instructions');
    await handoff.waitFor();
    assert.deepEqual(JSON.parse(await handoff.inputValue()), {
      name: 'room_upload_start', arguments: { room_id: 'r1', manifest, options: {} },
    });
    await page.getByText(/same registered agent/).waitFor();
    await page.getByLabel('Agent-created upload ID').fill('agent-u1');
    await page.getByRole('button', { name: 'Track upload', exact: true }).click();
    await page.getByText('Tracking upload.', { exact: true }).waitFor();
    assert.equal(calls.filter(c => c.path === '/rooms/r1/uploads').length, 0);
    assert.ok(calls.some(c => c.path === '/uploads/agent-u1'));
    assert.equal(await page.getByText('unfinished.csv').count(), 0);
    files[2].status = 'available'; batch.status = 'available'; batch.uploaded_bytes = 100;
    await page.getByRole('button', { name: 'Refresh files', exact: true }).click();
    await page.getByText('unfinished.csv', { exact: true }).waitFor();
    assert.deepEqual(calls.find(c => c.path.endsWith('/uploads/preview')).body, { manifest });
  }));

  test(`files ${surface}: pending status cannot block initial files, pagination or refresh`, async () => {
    let release;
    const statusResponse = new Promise(resolve => { release = resolve; });
    try {
      await withFiles(surface, async ({ page, calls, batch }) => {
        await page.locator('[data-file-path="data"]').waitFor();
        assert.equal(await page.getByRole('button', { name: 'Refresh files', exact: true }).isEnabled(), true);
        await page.getByRole('button', { name: 'Load more files' }).click();
        await page.getByText('last.txt', { exact: true }).waitFor();
        await page.getByRole('button', { name: 'Refresh files', exact: true }).click();
        await page.waitForFunction(() => !document.querySelector('[data-refresh-files]').disabled);
        assert.equal(calls.filter(c => c.path === '/uploads/agent-u1').length, 1, 'no overlapping status polls');
        release({ ...batch, uploaded_bytes: 75 });
        await page.waitForFunction(() => document.querySelector('progress').value === 75);
      }, { statusResponse });
    } finally { release({ http: 503, error: 'test ended' }); }
  });

  test(`files ${surface}: owners manage existing member permission controls`, async () => withFiles(surface, async ({ page, calls }) => {
    await page.getByRole('button', { name: 'Manage file permissions' }).click();
    await page.locator('[data-upload="member"]').check();
    await page.getByText('Upload permission saved.').waitFor();
    assert.equal(calls.find(c => c.path.endsWith('/upload-permission')).body.file_upload_permission, true);
  }));

  test(`files ${surface}: member permission is independent of chat role and cannot be escalated`, async () => withFiles(surface, async ({ page, calls }) => {
    await page.getByText(/Ask the room owner to enable uploads/).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Manage file permissions' }).count(), 0);
    assert.equal(await page.getByRole('button', { name: 'Start upload', exact: true }).count(), 0);
    await page.getByLabel('Upload manifest').fill(JSON.stringify(manifest));
    await page.getByRole('button', { name: 'Preview upload', exact: true }).click();
    await page.getByText('1 files · 3 bytes', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Start upload', exact: true }).count(), 0);
    assert.equal(calls.filter(c => c.path === '/rooms/r1/uploads').length, 0);
  }, { role: 'contributor', canUpload: false }));

  test(`files ${surface}: obsolete access and preview responses cannot affect a replacement room view`, async () => withFiles(surface, async ({ page, holds, calls }) => {
    await page.locator('[data-file-path="data"]').click();
    let release; holds.set('/rooms/r1/files/a/access', new Promise(r => { release = r; }));
    await page.locator('[data-file-id="a"]').getByRole('button', { name: 'Preview', exact: true }).click();
    await page.getByRole('tab', { name: '＋ New room', exact: true }).click();
    release({ url: 'https://objects.example/authorized', expires_at: Date.now() / 1000 + 60 });
    await page.locator('[data-room="r1"]').click();
    await page.getByRole('tab', { name: 'Files', exact: true }).click();
    assert.equal(await page.getByText(/Preview authorized/).count(), 0);
    assert.equal(calls.filter(c => c.path.endsWith('/access')).length, 1);
  }));

  test(`files ${surface}: upload status failure leaves finalized files usable`, async () => withFiles(surface, async ({ page, holds }) => {
    holds.set('/uploads/agent-u1', Promise.resolve({ http: 503, error: 'Upload status unavailable' }));
    await page.getByRole('button', { name: 'Refresh files', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: 'Upload status unavailable' }).waitFor();
    await page.locator('[data-file-path="data"]').click();
    await page.locator('[data-file-id="a"]').waitFor();
    holds.delete('/uploads/agent-u1');
    await page.getByRole('button', { name: 'Refresh files', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('[data-upload-status-error]').textContent === '');
    assert.equal(await page.getByRole('alert').filter({ hasText: 'Upload status unavailable' }).count(), 0);
  }));

  test(`files ${surface}: changing the manifest invalidates a successful dry run`, async () => withFiles(surface, async ({ page, calls }) => {
    await page.getByLabel('Upload manifest').fill(JSON.stringify(manifest));
    await page.getByRole('button', { name: 'Preview upload', exact: true }).click();
    await page.getByLabel('Agent upload instructions').waitFor();
    await page.getByLabel('Upload manifest').fill('invalid');
    assert.equal(await page.locator('[data-upload-summary]').textContent(), '');
    assert.equal(await page.getByLabel('Agent upload instructions').count(), 0);
    assert.equal(await page.getByRole('button', { name: 'Start upload', exact: true }).count(), 0);
    await page.getByRole('button', { name: 'Preview upload', exact: true }).click();
    await page.locator('[data-upload-message][role="alert"]').waitFor();
    assert.equal(calls.filter(c => c.path.endsWith('/uploads/preview')).length, 1);
    assert.equal(calls.filter(c => c.path === '/rooms/r1/uploads').length, 0);
  }));

  for (const operation of ['preview', 'status']) for (const outcome of ['success', 'failure']) {
    test(`files ${surface}: ${operation} ${outcome} after disposal has no UI effects or followups`, async () => withFiles(surface, async ({ page, holds, calls }) => {
      await page.locator('[data-file-path="data"]').waitFor();
      await page.waitForFunction(() => document.querySelector('[data-upload-status-loading]').textContent === '');
      const path = operation === 'preview' ? '/rooms/r1/uploads/preview' : '/uploads/agent-u1';
      let release; holds.set(path, new Promise(resolve => { release = resolve; }));
      const requestStarted = page.waitForRequest(request => new URL(request.url()).pathname === `/api/v1/collab${path}`);
      if (operation === 'preview') {
        await page.getByLabel('Upload manifest').fill(JSON.stringify(manifest));
        await page.getByRole('button', { name: 'Preview upload', exact: true }).click();
      } else {
        await page.getByLabel('Agent-created upload ID').fill('agent-u1');
        await page.getByRole('button', { name: 'Track upload', exact: true }).click();
      }
      await requestStarted;
      if (operation === 'preview') {
        await page.locator('[data-upload-preview]').dispatchEvent('submit');
        assert.equal(calls.filter(call => call.path === path).length, 1);
        assert.equal(await page.getByRole('button', { name: 'Preview upload', exact: true }).isDisabled(), true);
      }
      await page.evaluate(() => window.view.dispose());
      const before = await page.locator('#hcn-p-files').innerHTML(), count = calls.length;
      release(outcome === 'failure' ? { http: 403, error: 'Permission revoked' } : operation === 'preview'
        ? { room_id: 'r1', can_upload: true, file_count: 1, total_bytes: 3 }
        : { room_id: 'r1', upload_id: 'agent-u1', status: 'available', file_count: 1, total_bytes: 3 });
      await page.waitForTimeout(150);
      assert.equal(await page.locator('#hcn-p-files').innerHTML(), before);
      assert.equal(calls.length, count);
    }));
  }

  test(`files ${surface}: service failure is distinct from empty and refresh retries`, async () => withFiles(surface, async ({ page, holds }) => {
    holds.set('/rooms/r1/files', Promise.resolve({ http: 503, error: 'Files unavailable' }));
    await page.getByRole('button', { name: 'Refresh files', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: 'Files unavailable' }).waitFor();
    holds.delete('/rooms/r1/files');
    await page.getByRole('button', { name: 'Refresh files', exact: true }).click();
    await page.locator('[data-file-path="data"]').waitFor();
    assert.equal(await page.getByRole('alert').filter({ hasText: 'Files unavailable' }).count(), 0);
  }));
}
