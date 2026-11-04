import test from 'node:test';
import assert from 'node:assert/strict';
import { HerdrClient } from './client.js';
import { CollabApi, AuthError } from './collab.js';

const ok = body => ({ ok: true, status: 200, text: async () => JSON.stringify(body) });

function recorder(handler = () => ({})) {
  const calls = [];
  const client = new HerdrClient('http://h', { fetchImpl: async (url, options) => {
    const call = { url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : undefined, signal: options.signal };
    calls.push(call);
    return ok(handler(call));
  } });
  return { client, calls };
}

test('collaboration methods hit the exact paths, methods and bodies with encoded ids and the given signal', async () => {
  const { client, calls } = recorder();
  const { signal } = new AbortController();
  const base = 'http://h/api/v1/artifacts/a%2F1';
  await client.listArtifactCollaborators('a/1', { signal });
  await client.grantArtifactCollaborator('a/1', { agent_id: 'ag:1', permission: 'write', expires_in_days: 7 }, { signal });
  await client.revokeArtifactCollaborator('a/1', 'ag/1', { signal });
  await client.grantArtifactInvite('a/1', { invite_id: 'inv:1', permission: 'read', expires_in_days: 30 }, { signal });
  await client.artifactCollabActivity('a/1', { signal });
  await client.artifactVersionDiff('a/1', 'v/2', { signal });
  await client.restrictArtifactCollab('a/1', { signal });
  assert.deepEqual(calls.map(c => [c.method, c.url]), [
    ['GET', `${base}/collaborators`],
    ['POST', `${base}/collaborators`],
    ['POST', `${base}/collaborators/ag%2F1/revoke`],
    ['POST', `${base}/collaborators/invite-grant`],
    ['GET', `${base}/collab-activity`],
    ['GET', `${base}/versions/v%2F2/diff`],
    ['POST', `${base}/collab-restrict`],
  ]);
  assert.deepEqual(calls[1].body, { agent_id: 'ag:1', permission: 'write', expires_in_days: 7 });
  assert.deepEqual(calls[2].body, {});
  assert.deepEqual(calls[3].body, { invite_id: 'inv:1', permission: 'read', expires_in_days: 30 });
  assert.deepEqual(calls[6].body, {});
  for (const c of calls) assert.equal(c.signal, signal);
});

test('collaboration methods work without an options argument', async () => {
  const { client, calls } = recorder();
  await client.listArtifactCollaborators('a');
  await client.restrictArtifactCollab('a');
  assert.equal(calls.length, 2);
});

test('a 503 surfaces as an ApiError with status 503', async () => {
  const client = new HerdrClient('http://h', { fetchImpl: async () => ({ ok: false, status: 503, text: async () => '{"error":"collaboration is not enabled"}', headers: new Headers() }) });
  await assert.rejects(client.listArtifactCollaborators('a'), e => e.status === 503);
});

test('token-less CollabApi sends no Authorization header and uses the injected fetch on the collab base', async () => {
  const seen = [];
  const fetchFn = async (url, init) => { seen.push({ url, init }); return { ok: true, status: 200, json: async () => ({}) }; };
  const api = new CollabApi(null, fetchFn);
  await api.agents();
  await api.rooms();
  await api.inviteExternal('r 1', { label: 'x' });
  assert.deepEqual(seen.map(s => [s.init.method, s.url]), [
    ['GET', '/api/v1/collab/agents'], ['GET', '/api/v1/collab/rooms'], ['POST', '/api/v1/collab/rooms/r%201/externals/invites'],
  ]);
  for (const s of seen) assert.equal(Object.keys(s.init.headers).some(k => k.toLowerCase() === 'authorization'), false);
  assert.equal(seen[2].init.headers['Content-Type'], 'application/json');
});

test('token mode still sends the bearer token and signs out without one', async () => {
  const seen = [];
  const fetchFn = async (url, init) => { seen.push(init); return { ok: true, status: 200, json: async () => ({}) }; };
  await new CollabApi(() => ({ access: 'T' }), fetchFn).agents();
  assert.equal(seen[0].headers.Authorization, 'Bearer T');
  await assert.rejects(new CollabApi(() => null, fetchFn).agents(), AuthError);
  assert.equal(seen.length, 1);
});
