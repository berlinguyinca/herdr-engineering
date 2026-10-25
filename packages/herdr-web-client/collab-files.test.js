import test from 'node:test';
import assert from 'node:assert/strict';
import { CollabApi } from './collab.js';

const recorder = (reply) => {
  const seen = [];
  const f = async (url, init) => { seen.push({ url, ...init }); return reply(url, init); };
  return { seen, api: new CollabApi(() => ({ access: 'T' }), f) };
};
const ok = (json, blob) => ({ ok: true, status: 200, json: async () => json, blob: async () => blob });

test('an upload is raw octet-stream bytes, a version goes to the file it extends', async () => {
  const { seen, api } = recorder(() => ok({ file: { id: 'f1', version: 1 } }));
  const file = new Blob(['hello']); file.name = 'a b.txt';
  assert.deepEqual(await api.uploadFile('r 1', file), { id: 'f1', version: 1 });
  assert.equal(seen[0].url, '/api/v1/collab/rooms/r%201/files?name=a%20b.txt');
  assert.equal(seen[0].method, 'POST');
  assert.equal(seen[0].headers['Content-Type'], 'application/octet-stream');
  assert.equal(seen[0].headers.Authorization, 'Bearer T');
  assert.equal(seen[0].body, file);
  await api.uploadFile('r', file, 'f1');
  assert.equal(seen[1].url, '/api/v1/collab/rooms/r/files/f1/versions');
});

test('a download returns a blob; errors carry the server message', async () => {
  const blob = new Blob(['x']);
  const { seen, api } = recorder((url) => (url.includes('missing') ? { ok: false, status: 413, json: async () => ({ error: 'the limit is 25 MiB per file' }) } : ok({}, blob)));
  assert.equal(await api.downloadFile('r', 'f1', 2), blob);
  assert.equal(seen[0].url, '/api/v1/collab/rooms/r/files/f1?v=2');
  assert.equal(seen[0].headers['Content-Type'], undefined);
  await assert.rejects(api.downloadFile('r', 'missing'), /25 MiB per file/);
});

test('a message names its files by id only', async () => {
  const { seen, api } = recorder(() => ok({}));
  await api.say('r', 'see', undefined, ['f1']);
  assert.deepEqual(JSON.parse(seen[0].body).files, ['f1']);
  await api.say('r', 'no files');
  assert.equal(JSON.parse(seen[1].body).files, undefined);
});
