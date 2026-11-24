import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiError, HerdrClient } from './client.js';

const ok = body => ({ ok: true, status: 200, text: async () => JSON.stringify(body) });

function recorder(handler = () => ({})) {
  const calls = [];
  const client = new HerdrClient('http://h', { fetchImpl: async (url, options) => {
    calls.push({ url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : undefined, signal: options.signal });
    const result = handler(calls.at(-1));
    return result && 'ok' in result ? result : ok(result);
  } });
  return { client, calls };
}

test('delete helpers hit the fixed owner routes with exact bodies', async () => {
  const { client, calls } = recorder();
  const controller = new AbortController();
  await client.artifactDeleteAccess({ signal: controller.signal });
  await client.listTombstonedArtifacts();
  await client.deleteArtifacts(['a/1', 'b'], { reason: 'tidy', holdDays: 7, requestKey: 'k1' });
  await client.deleteArtifacts(['a/1', 'b'], { mode: 'purge_now', confirm: '2', reason: 'gone' });
  await client.restoreArtifacts(['a/1'], { requestKey: 'k2', signal: controller.signal });
  assert.deepEqual(calls.map(c => [c.method, c.url]), [
    ['GET', 'http://h/api/v1/artifacts/delete-access'],
    ['GET', 'http://h/api/v1/artifacts/tombstoned'],
    ['POST', 'http://h/api/v1/artifacts/delete'],
    ['POST', 'http://h/api/v1/artifacts/delete'],
    ['POST', 'http://h/api/v1/artifacts/restore'],
  ]);
  assert.equal(calls[0].signal, controller.signal);
  assert.deepEqual(calls[2].body, { ids: ['a/1', 'b'], mode: 'tombstone', reason: 'tidy', hold_days: 7, request_key: 'k1' });
  assert.deepEqual(calls[3].body, { ids: ['a/1', 'b'], mode: 'purge_now', reason: 'gone', confirm: '2' });
  assert.deepEqual(calls[4].body, { ids: ['a/1'], request_key: 'k2' });
});

test('a purge without its typed count never leaves the browser, and ids are capped at 200', async () => {
  const { client, calls } = recorder();
  await assert.rejects(client.deleteArtifacts(['a', 'b'], { mode: 'purge_now', reason: 'x' }), /confirm/);
  await assert.rejects(client.deleteArtifacts(['a', 'b'], { mode: 'purge_now', reason: 'x', confirm: 2 }), /confirm/);
  await assert.rejects(client.deleteArtifacts(Array.from({ length: 201 }, (_, n) => `a${n}`), { reason: 'x' }), /200/);
  await assert.rejects(client.deleteArtifacts([], { reason: 'x' }), /at least one/);
  await assert.rejects(client.restoreArtifacts(Array.from({ length: 201 }, (_, n) => `a${n}`)), /200/);
  assert.equal(calls.length, 0);
});

test('a refusal surfaces as an ApiError carrying the status', async () => {
  const { client } = recorder(() => ({ ok: false, status: 404, text: async () => JSON.stringify({ error: 'not found' }), headers: new Headers() }));
  await assert.rejects(client.deleteArtifacts(['a'], { reason: 'x' }), error => error instanceof ApiError && error.status === 404);
});
