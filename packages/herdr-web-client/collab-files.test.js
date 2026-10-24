import test from 'node:test';
import assert from 'node:assert/strict';
import { CollabApi } from './collab.js';

const module = () => import('../collab-files.js');
const files = [
  { file_id: 'a', room_id: 'r/1', relative_path: 'data/a.csv', size_bytes: 12, status: 'available' },
  { file_id: 'b', room_id: 'r/1', relative_path: 'data/deep/b.json', size_bytes: 9, status: 'available' },
  { file_id: 'c', room_id: 'r/1', relative_path: 'other.txt', size_bytes: 2, status: 'available' },
  ...['created', 'uploading', 'verifying', 'failed'].map(status => ({ file_id: status, relative_path: `secret-${status}`, status })),
];

test('file browser navigates immediate paths and searches loaded finalized records only', async () => {
  const { renderFileBrowser } = await module();
  const root = renderFileBrowser(files, { path: '', query: '' });
  assert.match(root, /data-file-path="data"/);
  assert.doesNotMatch(root, /data-file-id="a"|secret-/);
  const nested = renderFileBrowser(files, { path: 'data', query: '' });
  assert.match(nested, /data-file-id="a"/);
  assert.match(nested, /data-file-path="data\/deep"/);
  assert.doesNotMatch(nested, /data-file-id="b"|other.txt/);
  const search = renderFileBrowser(files, { path: '', query: 'B.JSON' });
  assert.match(search, /data\/deep\/b.json/);
  assert.doesNotMatch(search, /data-file-id="a"|secret-/);
});

test('file browser escapes metadata, separates preview states, and distinguishes loading/error/empty', async () => {
  const { renderFileBrowser } = await module();
  const html = renderFileBrowser(['pending', 'failed', 'available'].map((preview_status, i) => ({
    file_id: String(i), room_id: 'r', relative_path: `<img onerror="bad()">${i}`, status: 'available', preview_status,
  })), {});
  assert.doesNotMatch(html, /<img/);
  for (const status of ['pending', 'failed', 'available']) assert.match(html, new RegExp(`Preview ${status}`));
  assert.match(renderFileBrowser([], { status: 'loading' }), /Loading/);
  assert.match(renderFileBrowser([], { error: 'Service unavailable' }), /role="alert".*Service unavailable/s);
  assert.match(renderFileBrowser([], {}), /No files/);
});

test('upload status renders one aggregate, clamps progress, and does not reveal grant secrets', async () => {
  const { renderUploadBatch } = await module();
  for (const status of ['created', 'uploading', 'verifying', 'available', 'failed']) {
    const html = renderUploadBatch({ upload_id: 'u', status, file_count: 10000, total_bytes: 100, uploaded_bytes: 50,
      completed_parts: 1, total_parts: 2, files: [{ url: 'SECRET-GRANT' }] });
    assert.match(html, new RegExp(status));
    assert.match(html, /10000 files/);
    assert.match(html, /value="50"/);
    assert.equal((html.match(/<progress/g) || []).length, 1);
    assert.doesNotMatch(html, /SECRET-GRANT/);
  }
  assert.match(renderUploadBatch({ total_bytes: 2, uploaded_bytes: 9 }), /value="100"/);
  assert.doesNotMatch(renderUploadBatch({ total_bytes: 0, uploaded_bytes: 0 }), /NaN|Infinity/);
});

test('authorized file entry rechecks the encoded room/file via shared authenticated transport', async () => {
  const { openAuthorizedFile } = await module();
  const calls = [];
  const api = new CollabApi(() => ({ access: 'token' }), async (url, init) => {
    calls.push({ url, ...init });
    return { ok: true, status: 200, json: async () => ({ url: 'https://objects.example/short-lived', expires_at: Date.now() / 1000 + 60 }) };
  });
  for (const purpose of ['download', 'preview']) {
    const result = await openAuthorizedFile({ ...files[0], file_id: 'f/1', url: 'https://untrusted.example' }, { api, purpose });
    assert.equal(result.url, 'https://objects.example/short-lived');
    assert.equal(calls.at(-1).url, '/api/v1/collab/rooms/r%2F1/files/f%2F1/access');
    assert.equal(calls.at(-1).headers.Authorization, 'Bearer token');
    assert.deepEqual(JSON.parse(calls.at(-1).body), { purpose });
  }
  await assert.rejects(openAuthorizedFile({ ...files[0], status: 'verifying' }, { api }), /available|finalized/);
  assert.equal(calls.length, 2);
});

test('authorized file entry rejects denied, expired and executable sources', async () => {
  const { openAuthorizedFile } = await module();
  for (const grant of [
    { url: 'javascript:alert(1)', expires_at: Date.now() / 1000 + 60 },
    { url: 'https://objects.example/file', expires_at: 1 },
    { url: 'https://objects.example/file' },
  ]) {
    const api = new CollabApi(() => ({ access: 'token' }), async () => ({ ok: true, status: 200, json: async () => grant }));
    await assert.rejects(openAuthorizedFile(files[0], { api }), /authorized|expired|secure/i);
  }
  const api = new CollabApi(() => ({ access: 'token' }), async () => ({ ok: false, status: 403, json: async () => ({ error: 'Room access revoked' }) }));
  await assert.rejects(openAuthorizedFile(files[0], { api }), /Room access revoked/);
});

test('browser upload preview preserves the manifest without creating a user-owned session', async () => {
  const { startBulkUploadPreview } = await module();
  const manifest = { files: [{ relative_path: 'a.csv', size_bytes: 3, sha256: 'a'.repeat(64), parts: [{ size_bytes: 3, sha256: 'a'.repeat(64) }] }] };
  const calls = [];
  const api = new CollabApi(() => ({ access: 'token' }), async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body) });
    return { ok: true, status: 200, json: async () => ({ room_id: 'r/1', file_count: 1, total_bytes: 3, can_upload: true, access_request_required: false }) };
  });
  assert.equal((await startBulkUploadPreview('r/1', manifest, { api })).file_count, 1);
  assert.equal(api.startUpload, undefined, 'session creation belongs to the registered agent, not the browser client');
  assert.deepEqual(calls, [
    { url: '/api/v1/collab/rooms/r%2F1/uploads/preview', body: { manifest } },
  ]);
  await assert.rejects(startBulkUploadPreview('r/1', '/data/**/*.csv', { api }), /manifest/i);
  assert.equal(calls.length, 1);
});
