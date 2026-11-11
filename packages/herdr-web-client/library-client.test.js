import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiError, HerdrClient } from './client.js';

const ok = (body) => ({ ok: true, status: 200, text: async () => JSON.stringify(body) });

function recorder(handler = () => ({})) {
  const calls = [];
  const client = new HerdrClient('http://h', { fetchImpl: async (url, options) => {
    calls.push({ url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : undefined });
    const result = handler(calls.at(-1));
    return result && 'ok' in result ? result : ok(result);
  } });
  return { client, calls };
}

test('listLibrary builds the query string and escapes q', async () => {
  const { client, calls } = recorder();
  await client.listLibrary({ q: 'a&b=c #1 %', sources: ['mine', 'pending'], folderId: 'fld_1', type: 'web', since: '2026-01-01T00:00:00Z', limit: 20, offset: 40 });
  const url = new URL(calls[0].url);
  assert.equal(url.pathname, '/api/v1/library');
  assert.equal(url.searchParams.get('q'), 'a&b=c #1 %');
  assert.ok(calls[0].url.includes('q=a%26b%3Dc+%231+%25'));
  assert.equal(url.searchParams.get('sources'), 'mine,pending');
  assert.equal(url.searchParams.get('folder_id'), 'fld_1');
  assert.equal(url.searchParams.get('type'), 'web');
  assert.equal(url.searchParams.get('limit'), '20');
  assert.equal(url.searchParams.get('offset'), '40');
  await client.listLibrary();
  assert.equal(calls[1].url, 'http://h/api/v1/library');
  await client.listLibrary({ q: '', sources: [] });
  assert.equal(calls[2].url, 'http://h/api/v1/library');
});

test('library writes hit the fixed routes with exact methods and bodies', async () => {
  const { client, calls } = recorder();
  await client.createFolder('Reading');
  await client.renameFolder('fld/1', 'To read');
  await client.deleteFolder('fld/1');
  await client.fileItem('fld_1', 'art 1');
  await client.unfileItem('fld/1', 'art 1');
  await client.acceptPending('a/1');
  await client.discardPending('a/1');
  assert.deepEqual(calls.map((c) => [c.method, c.url]), [
    ['POST', 'http://h/api/v1/library/folders'],
    ['PUT', 'http://h/api/v1/library/folders/fld%2F1'],
    ['DELETE', 'http://h/api/v1/library/folders/fld%2F1'],
    ['POST', 'http://h/api/v1/library/items'],
    ['DELETE', 'http://h/api/v1/library/folders/fld%2F1/items/art%201'],
    ['POST', 'http://h/api/v1/library/pending/a%2F1/accept'],
    ['POST', 'http://h/api/v1/library/pending/a%2F1/discard'],
  ]);
  assert.deepEqual(calls[0].body, { name: 'Reading' });
  assert.deepEqual(calls[1].body, { name: 'To read' });
  assert.deepEqual(calls[3].body, { folder_id: 'fld_1', artifact_id: 'art 1' });
  assert.equal(calls[2].body, undefined);
});

test('a library refusal keeps its message and status', async () => {
  const { client } = recorder(() => ({ ok: false, status: 429, headers: new Headers(), text: async () => JSON.stringify({ error: 'pending_cap', message: 'You have 25 pending artifacts' }) }));
  await assert.rejects(client.acceptPending('a'), (e) => e instanceof ApiError && e.status === 429 && e.body?.message === 'You have 25 pending artifacts');
});
