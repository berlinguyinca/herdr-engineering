import test from 'node:test';
import assert from 'node:assert/strict';
import { HerdrClient } from './client.js';

const ok = body => ({ ok: true, status: 200, text: async () => JSON.stringify(body) });

function recorder(handler = () => ({})) {
  const calls = [];
  const client = new HerdrClient('http://h', { fetchImpl: async (url, options) => {
    const call = { url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : undefined, signal: options.signal };
    calls.push(call);
    const result = handler(call);
    return result && 'ok' in result ? result : ok(result);
  } });
  return { client, calls };
}

test('share methods hit the exact paths, methods and bodies with encoded ids', async () => {
  const { client, calls } = recorder();
  const controller = new AbortController();
  const body = { version_id: 'v/1', audience: 'lab' };
  await client.listArtifactShares('a/1');
  await client.createArtifactShare('a/1', body);
  await client.getArtifactShare('a/1', 's:1');
  await client.approveArtifactShare('a/1', 's:1', { signal: controller.signal });
  await client.revokeArtifactShare('a/1', 's:1');
  await client.artifactShareEvents('a/1', 's:1');
  const base = 'http://h/api/v1/artifacts/a%2F1/shares';
  assert.deepEqual(calls.map(c => [c.method, c.url]), [
    ['GET', base], ['POST', base], ['GET', `${base}/s%3A1`], ['POST', `${base}/s%3A1/approve`],
    ['POST', `${base}/s%3A1/revoke`], ['GET', `${base}/s%3A1/events`],
  ]);
  assert.deepEqual(calls[1].body, body);
  assert.deepEqual(calls[3].body, { confirm: true });
  assert.equal(calls[3].signal, controller.signal);
});

const head = { current_version_id: 'v-head', artifact_type: 'other' };
const target = { version_id: 'v/old', source_manifest_blob: 'blob:src', output_manifest: [{ name: 'a.txt', sha256: 'x' }] };

function restoreHandler(overrides = {}) {
  return call => {
    if (call.method === 'POST') return overrides.post ? overrides.post(call) : { version_id: 'v-new' };
    if (call.url.includes('/versions/')) return overrides.target || target;
    return overrides.head || head;
  };
}

test('restoreArtifactVersion commits the old manifests on top of the current head', async () => {
  const { client, calls } = recorder(restoreHandler());
  const result = await client.restoreArtifactVersion('a/1', 'v/old');
  assert.deepEqual(result, { version_id: 'v-new' });
  const post = calls.find(c => c.method === 'POST');
  assert.equal(post.url, 'http://h/api/v1/artifacts/a%2F1/versions');
  assert.deepEqual(post.body, {
    base_version_id: 'v-head',
    source_manifest_blob: 'blob:src',
    output_manifest: target.output_manifest,
    provenance: { restored_from: 'v/old' },
    idempotency_key: 'restore-v/old-v-head',
  });
  assert.ok(calls.some(c => c.url === 'http://h/api/v1/artifacts/a%2F1/versions/v%2Fold'));
});

test('restoreArtifactVersion refuses report artifacts and report versions without posting', async () => {
  for (const overrides of [
    { head: { ...head, artifact_type: 'report' } },
    { target: { ...target, report_descriptor: { assets: [] } } },
  ]) {
    const { client, calls } = recorder(restoreHandler(overrides));
    await assert.rejects(client.restoreArtifactVersion('a', 'v/old'), error => error.code === 'restore_unsupported');
    assert.ok(calls.every(c => c.method === 'GET'));
  }
});

test('restoreArtifactVersion lets a stale-head 409 propagate', async () => {
  const { client } = recorder(restoreHandler({ post: () => ({
    ok: false, status: 409, text: async () => JSON.stringify({ error: 'conflict' }),
  }) }));
  await assert.rejects(client.restoreArtifactVersion('a', 'v/old'), error => error.status === 409);
});
