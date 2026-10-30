import test from 'node:test';
import assert from 'node:assert/strict';
import { HerdrClient } from './client.js';

test('fabric routes preserve encoded identities, filters, cancellation and write bodies', async () => {
  const calls = [];
  const client = new HerdrClient('/fabric', { fetchImpl: async (url, options) => {
    calls.push({ url, ...options });
    return { ok: true, text: async () => '{}' };
  } });
  const signal = new AbortController().signal;
  await client.getHandoffTree({ signal });
  await client.listHandoffs('in progress', { signal });
  await client.listArtifacts({ q: 'a & b', type: 'document', offset: 0, limit: 20, signal });
  await client.getArtifact('a/b', { signal });
  await client.artifactVersions('a/b', { signal });
  await client.artifactVersion('a/b', 'v/1', { signal });
  await client.artifactReviews('a/b', { signal });
  await client.requestArtifactReview('a/b', 'v/1', { signal });
  await client.completeArtifactReview('r/1', 'approve', ['ready'], { signal });
  await client.listPublications({ signal });
  await client.approvePublication('p/1', { signal });
  await client.revokePublication('p/1', { signal });
  await client.schedulerOverview({ signal });
  await client.schedulerPlan({ request_id: 'preview-1' }, { signal });
  assert.deepEqual(calls.map(c => c.url), [
    '/fabric/api/v1/handoffs/tree', '/fabric/api/v1/handoffs?state=in%20progress',
    '/fabric/api/v1/artifacts?q=a+%26+b&type=document&offset=0&limit=20',
    '/fabric/api/v1/artifacts/a%2Fb', '/fabric/api/v1/artifacts/a%2Fb/versions',
    '/fabric/api/v1/artifacts/a%2Fb/versions/v%2F1', '/fabric/api/v1/artifacts/a%2Fb/reviews',
    '/fabric/api/v1/artifact-reviews', '/fabric/api/v1/artifact-reviews/r%2F1/complete',
    '/fabric/api/v1/publications', '/fabric/api/v1/publications/p%2F1/approve',
    '/fabric/api/v1/publications/p%2F1/revoke', '/fabric/api/v1/scheduler', '/fabric/api/v1/scheduler/plan',
  ]);
  assert.ok(calls.every(c => c.signal === signal));
  assert.deepEqual(calls.filter(c => c.method === 'POST').map(c => JSON.parse(c.body)), [
    { artifact_id: 'a/b', version_id: 'v/1' }, { decision: 'approve', comments: ['ready'] }, {}, {}, { request_id: 'preview-1' },
  ]);
  assert.equal(client.artifactDownloadUrl('a/b'), '/fabric/api/v1/artifacts/a%2Fb/download');
});

test('handoff overview, paging, detail and chain routes encode their inputs', async () => {
  const calls = [];
  const client = new HerdrClient('/fabric', { fetchImpl: async (url) => { calls.push(url); return { ok: true, text: async () => '{}' }; } });
  await client.getHandoffOverview();
  await client.pageHandoffs({ workspace: 'org/app & co', state: 'failed', attention: true, cursor: 'a|b', limit: 25 });
  await client.pageHandoffs();
  await client.getHandoff('h/1');
  await client.getHandoffChain('h/1');
  assert.deepEqual(calls, ['/fabric/api/v1/handoffs/overview',
    '/fabric/api/v1/handoffs?limit=25&state=failed&workspace=org%2Fapp+%26+co&attention=1&cursor=a%7Cb',
    '/fabric/api/v1/handoffs?limit=50', '/fabric/api/v1/handoffs/h%2F1', '/fabric/api/v1/handoffs/h%2F1/chain']);
});
