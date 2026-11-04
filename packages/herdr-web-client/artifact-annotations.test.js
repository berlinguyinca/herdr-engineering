import test from 'node:test';
import assert from 'node:assert/strict';
import { HerdrClient } from './client.js';

const ok = body => ({ ok: true, status: 200, text: async () => JSON.stringify(body) });

function recorder() {
  const calls = [];
  const client = new HerdrClient('http://h', { fetchImpl: async (url, options) => {
    calls.push({ url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : undefined, signal: options.signal });
    return ok({});
  } });
  return { client, calls };
}

test('listArtifactAnnotations defaults to open and encodes ids, status and cursor', async () => {
  const { client, calls } = recorder();
  const controller = new AbortController();
  await client.listArtifactAnnotations('a/1');
  await client.listArtifactAnnotations('a/1', { before: null });
  await client.listArtifactAnnotations('a/1', { status: 'withdrawn', before: '2026-10-30T10:00:00+00:00|ann_x', signal: controller.signal });
  const base = 'http://h/api/v1/artifacts/a%2F1/annotations';
  assert.deepEqual(calls.map(c => [c.method, c.url]), [
    ['GET', `${base}?status=open`], ['GET', `${base}?status=open`],
    ['GET', `${base}?status=withdrawn&before=2026-10-30T10%3A00%3A00%2B00%3A00%7Cann_x`],
  ]);
  assert.equal(calls[0].body, undefined);
  assert.equal(calls[2].signal, controller.signal);
});

test('dismissArtifactAnnotation POSTs an empty object to the encoded dismiss path', async () => {
  const { client, calls } = recorder();
  const controller = new AbortController();
  await client.dismissArtifactAnnotation('a/1', 'ann:1', { signal: controller.signal });
  assert.deepEqual(calls.map(c => [c.method, c.url]), [['POST', 'http://h/api/v1/artifacts/a%2F1/annotations/ann%3A1/dismiss']]);
  assert.deepEqual(calls[0].body, {});
  assert.equal(calls[0].signal, controller.signal);
});
