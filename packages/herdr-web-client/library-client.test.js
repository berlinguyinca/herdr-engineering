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

test('the library client has no write call and reads the fixed routes', async () => {
  const { client, calls } = recorder();
  for (const name of ['createFolder', 'renameFolder', 'deleteFolder', 'fileItem', 'unfileItem', 'acceptPending', 'discardPending']) {
    assert.equal(client[name], undefined, name);
  }
  await client.libraryItem('a/1');
  await client.libraryVersions('a/1');
  await client.libraryShares('a/1');
  assert.deepEqual(calls.map((c) => [c.method, c.url]), [
    ['GET', 'http://h/api/v1/library/items/a%2F1'],
    ['GET', 'http://h/api/v1/library/items/a%2F1/versions'],
    ['GET', 'http://h/api/v1/library/shares?artifact_id=a%2F1'],
  ]);
});

test('libraryContentUrl encodes every piece and stays on the dashboard origin', () => {
  const { client } = recorder();
  assert.equal(client.libraryContentUrl('a1', 'v 1', 'data/in dex.html'), 'http://h/api/v1/library/content/a1/v%201/data/in%20dex.html');
});

test('a library refusal keeps its message and status', async () => {
  const { client } = recorder(() => ({ ok: false, status: 503, headers: new Headers(), text: async () => JSON.stringify({ error: 'library_unavailable', message: 'the artifact host is unavailable' }) }));
  await assert.rejects(client.listLibrary(), (e) => e instanceof ApiError && e.status === 503 && e.body?.message === 'the artifact host is unavailable');
});
