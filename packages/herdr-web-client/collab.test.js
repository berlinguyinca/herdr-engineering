import test from 'node:test';
import assert from 'node:assert/strict';
import { pkcePair, authorizeUrl, parseRedirect, exchangeCode, TokenStore, tokenExpiry, CollabApi, AuthError } from './collab.js';
import * as collab from './collab.js';

const cfg = { client_id: 'web', hosted_ui_domain: 'https://auth.example/' };
const jwt = (exp) => `h.${btoa(JSON.stringify({ exp })).replaceAll('+', '-').replaceAll('/', '_')}.s`;

const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => structuredClone(data) });

test('collab public and dashboard clients normalize identity and room capabilities identically', async () => {
  const identity = { user_id: 'u1', email: 'one@example.org', user_status: 'approved', approver: true };
  const room = { room_id: 'r1', name: 'Lab', role: 'contributor', user_status: 'approved',
    room_membership_status: 'approved', agent_permission_status: { a1: 'pending' }, file_upload_permission: false };
  for (const access of ['public-token', 'operator']) {
    const api = new CollabApi(() => ({ access }), async (url, init) => {
      assert.equal(init.headers.Authorization, `Bearer ${access}`);
      return response(url.endsWith('/me') ? identity : url.endsWith('/rooms') ? { rooms: [room] } : room);
    });
    const me = await api.me();
    assert.equal(me.status, 'approved');
    assert.deepEqual(me.capabilities, { canManagePeople: true });
    assert.deepEqual(await api.link('id-token'), me);
    const result = (await api.rooms()).rooms[0];
    assert.deepEqual(result, await api.room('r1'));
    assert.deepEqual(result.capabilities, { canRead: true, canPost: true, canInvite: false, canUpload: false });
    assert.deepEqual(result.agent_permission_status, { a1: 'pending' });
  }
});

test('collab room capabilities keep approval, membership and upload permission independent', () => {
  const room = { role: 'owner', user_status: 'approved', room_membership_status: 'approved', file_upload_permission: true };
  assert.deepEqual(collab.normalizeRoom(room).capabilities, { canRead: true, canPost: true, canInvite: true, canUpload: true });
  for (const change of [{ user_status: 'disabled' }, { room_membership_status: 'removed' }, { room_membership_status: 'pending' }]) {
    assert.deepEqual(collab.normalizeRoom({ ...room, ...change }).capabilities,
      { canRead: false, canPost: false, canInvite: false, canUpload: false });
  }
  for (const role of ['viewer', 'guest']) {
    assert.deepEqual(collab.normalizeRoom({ ...room, role }).capabilities,
      { canRead: true, canPost: false, canInvite: false, canUpload: true });
  }
  assert.equal(collab.normalizeRoom({ ...room, file_upload_permission: 'false' }).capabilities.canUpload, false);
  assert.deepEqual(collab.normalizeRoom({ role: 'contributor' }).capabilities,
    { canRead: true, canPost: true, canInvite: false, canUpload: false }, 'legacy roles remain usable without granting upload');
  assert.deepEqual(collab.normalizeRoom({}).capabilities,
    { canRead: false, canPost: false, canInvite: false, canUpload: false });
});

test('collab explicit user status wins over legacy status and admin cannot bypass approval', async () => {
  const api = new CollabApi(() => ({ access: 'operator' }), async () => response({
    user_id: 'u1', status: 'approved', user_status: 'disabled', admin: true,
  }));
  assert.equal((await api.me()).status, 'disabled');
  assert.equal((await api.me()).capabilities.canManagePeople, false);
});

test('collab roomFiles, uploadStatus and skills preserve data and encode identifiers and queries', async () => {
  const files = { files: [{ file_id: 'f1', room_id: 'r/1', relative_path: 'results/a.json', status: 'available' }], next_cursor: 'next', has_more: true };
  const upload = { upload_id: 'u/1', room_id: 'r/1', status: 'uploading', file_count: 1,
    total_bytes: 12, uploaded_bytes: 6, completed_parts: 1, total_parts: 2 };
  const skills = { skills: [{ skill_id: 's1', scope: 'room', version: 'v1' }] };
  for (const access of ['public-token', 'operator']) {
    const calls = [];
    const api = new CollabApi(() => ({ access }), async (url, init) => {
      calls.push([init.method, url]);
      return response(url.includes('/files') ? files : url.includes('/uploads/') ? upload : skills);
    });
    assert.equal(typeof api.roomFiles, 'function');
    assert.equal(typeof api.uploadStatus, 'function');
    assert.equal(typeof api.skills, 'function');
    assert.deepEqual(await api.roomFiles('r/1', 'a&b=1'), files);
    await api.roomFiles('r/1');
    await api.roomFiles('r/1', 0);
    assert.deepEqual(await api.uploadStatus('u/1'), upload);
    assert.deepEqual(await api.skills('room & shared'), skills);
    await api.skills();
    assert.deepEqual(calls, [
      ['GET', '/api/v1/collab/rooms/r%2F1/files?cursor=a%26b%3D1'],
      ['GET', '/api/v1/collab/rooms/r%2F1/files'],
      ['GET', '/api/v1/collab/rooms/r%2F1/files?cursor=0'],
      ['GET', '/api/v1/collab/uploads/u%2F1'],
      ['GET', '/api/v1/collab/skills?scope=room%20%26%20shared'],
      ['GET', '/api/v1/collab/skills'],
    ]);
  }
});

test('collab errors expose stable HTTP codes without losing messages or AuthError compatibility', async () => {
  for (const [status, code] of [[401, 'unauthorized'], [403, 'forbidden'], [404, 'not_found'],
    [409, 'conflict'], [429, 'rate_limited'], [503, 'unavailable'], [400, 'request_failed']]) {
    const api = new CollabApi(() => ({ access: 't' }), async () => response({ error: 'server message' }, status));
    await assert.rejects(api.me(), (error) => {
      assert.equal(error.status, status);
      assert.equal(error.code, code);
      if (status === 401) assert.ok(error instanceof AuthError);
      else assert.equal(error.message, 'server message');
      return true;
    });
  }
  await assert.rejects(new CollabApi(() => null).me(), { code: 'unauthorized', status: 401 });
});

test('collab network and malformed responses degrade while cancellation is preserved', async () => {
  const api = (fetchFn) => new CollabApi(() => ({ access: 't' }), fetchFn);
  await assert.rejects(api(async () => { throw new TypeError('offline'); }).me(), { code: 'network_error', status: 0 });
  const broken = (status) => ({ ok: status < 400, status, json: async () => { throw new SyntaxError('bad JSON'); } });
  await assert.rejects(api(async () => broken(503)).me(), { code: 'unavailable', status: 503 });
  await assert.rejects(api(async () => broken(200)).me(), { code: 'invalid_response', status: 200 });
  const abort = new DOMException('cancelled', 'AbortError');
  await assert.rejects(api(async () => { throw abort; }).me(), (error) => error === abort);
});

test('collab malformed room lists reject with the shared response error instead of appearing empty', async () => {
  for (const data of [{}, { rooms: null }, { rooms: 'bad' }, { rooms: [null] }]) {
    const api = new CollabApi(() => ({ access: 't' }), async () => response(data));
    await assert.rejects(api.rooms(), { code: 'invalid_response', status: 200 });
  }
});

test('PKCE challenge is the S256 of the verifier', async () => {
  const { verifier, challenge } = await pkcePair();
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  const expect = btoa(String.fromCharCode(...new Uint8Array(digest))).replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '');
  assert.equal(challenge, expect);
  assert.ok(verifier.length >= 43);
});

test('authorize URL carries PKCE, state and the registered redirect', () => {
  const u = new URL(authorizeUrl(cfg, 'https://h:9447/', 'st', 'ch'));
  assert.equal(u.origin + u.pathname, 'https://auth.example/oauth2/authorize');
  assert.equal(u.searchParams.get('code_challenge_method'), 'S256');
  assert.equal(u.searchParams.get('redirect_uri'), 'https://h:9447/');
  assert.equal(u.searchParams.get('state'), 'st');
  assert.equal(u.searchParams.get('response_type'), 'code');
});

test('redirect parsing handles code, error and noise', () => {
  assert.deepEqual(parseRedirect('?code=a&state=b'), { code: 'a', state: 'b' });
  assert.deepEqual(parseRedirect('?error=access_denied&error_description=nope'), { error: 'nope' });
  assert.equal(parseRedirect('?x=1'), null);
  assert.equal(parseRedirect('?code=a'), null); // state is mandatory
});

test('code exchange posts the verifier and returns both tokens', async () => {
  let seen;
  const f = async (url, init) => { seen = { url, body: init.body.toString() }; return { ok: true, json: async () => ({ access_token: 'A', id_token: 'I' }) }; };
  assert.deepEqual(await exchangeCode(cfg, 'c', 'v', 'https://h/', f), { access: 'A', id: 'I' });
  assert.match(seen.body, /code_verifier=v/);
  await assert.rejects(exchangeCode(cfg, 'c', 'v', 'r', async () => ({ ok: false })), /sign-in failed/);
});

test('token store drops expired tokens and uses only the given storage', () => {
  const mem = new Map();
  const storage = { getItem: (k) => mem.get(k) ?? null, setItem: (k, v) => mem.set(k, v), removeItem: (k) => mem.delete(k) };
  const s = new TokenStore(storage);
  s.set({ access: jwt(Date.now() / 1000 + 600), id: 'i' });
  assert.ok(s.get());
  s.set({ access: jwt(Date.now() / 1000 - 5), id: 'i' });
  assert.equal(s.get(), null);
  assert.equal(tokenExpiry('garbage'), 0);
});

test('agent endpoints use the documented routes', async () => {
  const calls = [];
  const f = async (url, init) => { calls.push([init.method, url, init.body]); return { status: 200, ok: true, json: async () => ({}) }; };
  const api = new CollabApi(() => ({ access: 'T' }), f);
  await api.connectAgent(); await api.revokeAgent('agent_1'); await api.grantAgent('room_1', 'agent_1', ['read']);
  assert.deepEqual(calls.map((c) => c.slice(0, 2)), [
    ['POST', '/api/v1/collab/agents/connect'], ['POST', '/api/v1/collab/agents/agent_1/revoke'],
    ['POST', '/api/v1/collab/rooms/room_1/agents']]);
  assert.deepEqual(JSON.parse(calls[2][2]), { agent_id: 'agent_1', actions: ['read'] });
});

test('agent permission, request and task calls use the documented routes', async () => {
  const calls = [];
  const f = async (url, init) => { calls.push([init.method, url, init.body]); return { status: 200, ok: true, json: async () => ({}) }; };
  const api = new CollabApi(() => ({ access: 'T' }), f);
  await api.connectAgent('alices-helper'); await api.roomAgents('r1'); await api.agentRequests('r1');
  await api.decideRequest('r1', 'g1', 'approve', ['read', 'execute']); await api.decideRequest('r1', 'g2', 'deny');
  await api.removeAgent('r1', 'a1'); await api.tasks('r1'); await api.createTask('r1', 'a1', 'do it'); await api.cancelTask('r1', 't1');
  assert.deepEqual(calls.map((c) => c.slice(0, 2)), [
    ['POST', '/api/v1/collab/agents/connect'], ['GET', '/api/v1/collab/rooms/r1/agents'],
    ['GET', '/api/v1/collab/rooms/r1/agent-requests'],
    ['POST', '/api/v1/collab/rooms/r1/agent-requests/g1/decide'], ['POST', '/api/v1/collab/rooms/r1/agent-requests/g2/decide'],
    ['POST', '/api/v1/collab/rooms/r1/agents/a1/remove'], ['GET', '/api/v1/collab/rooms/r1/tasks'],
    ['POST', '/api/v1/collab/rooms/r1/tasks'], ['POST', '/api/v1/collab/rooms/r1/tasks/t1/cancel']]);
  assert.deepEqual(JSON.parse(calls[0][2]), { harness_type: 'claude-code', name: 'alices-helper' });
  assert.deepEqual(JSON.parse(calls[3][2]), { decision: 'approve', actions: ['read', 'execute'] });
  assert.deepEqual(JSON.parse(calls[4][2]), { decision: 'deny' });
  assert.deepEqual(JSON.parse(calls[7][2]), { target_agent_id: 'a1', text: 'do it' });
});

test('API wrapper sends the bearer token and turns 401 into AuthError', async () => {
  const calls = [];
  const f = async (url, init) => { calls.push([url, init]); return url.endsWith('/rooms') ? { status: 401, ok: false, json: async () => ({}) } : { status: 200, ok: true, json: async () => ({ ok: 1 }) }; };
  const api = new CollabApi(() => ({ access: 'TOK' }), f);
  await api.invite('room_x', 'a@b.org', 'viewer');
  assert.equal(calls[0][1].headers.Authorization, 'Bearer TOK');
  assert.equal(calls[0][0], '/api/v1/collab/rooms/room_x/invites');
  await assert.rejects(api.rooms(), AuthError);
  await assert.rejects(new CollabApi(() => null, f).rooms(), AuthError);
});

test('renaming an agent posts the new name to the documented route', async () => {
  const calls = [];
  const f = async (url, init) => { calls.push([init.method, url, init.body]); return { status: 200, ok: true, json: async () => ({}) }; };
  const api = new CollabApi(() => ({ access: 'T' }), f);
  await api.renameAgent('agent 1', 'architect');
  assert.deepEqual(calls[0].slice(0, 2), ['POST', '/api/v1/collab/agents/agent%201/rename']);
  assert.deepEqual(JSON.parse(calls[0][2]), { name: 'architect' });
});

test('remote-agent calls use the documented routes', async () => {
  const calls = [];
  const f = async (url, init) => { calls.push([init.method, url, init.body]); return { status: 200, ok: true, json: async () => ({}) }; };
  const api = new CollabApi(() => ({ access: 'T' }), f);
  await api.externals('r 1'); await api.inviteExternal('r1', { label: 'Dana', actions: ['read'] });
  await api.revokeExternal('r1', 'agent_1'); await api.revokeExternalInvite('r1', 'collab_1');
  assert.deepEqual(calls.map((c) => c.slice(0, 2)), [
    ['GET', '/api/v1/collab/rooms/r%201/externals'], ['POST', '/api/v1/collab/rooms/r1/externals/invites'],
    ['POST', '/api/v1/collab/rooms/r1/externals/agent_1/revoke'],
    ['POST', '/api/v1/collab/rooms/r1/externals/invites/collab_1/revoke']]);
  assert.deepEqual(JSON.parse(calls[1][2]), { label: 'Dana', actions: ['read'] });
});

test('delegation and proposal calls use the documented owner routes and never an apply route', async () => {
  const calls = [];
  const f = async (url, init) => { calls.push([init.method, url, init.body]); return { status: 200, ok: true, json: async () => ({}) }; };
  const api = new CollabApi(() => ({ access: 'T' }), f);
  await api.delegations('r1'); await api.delegate('r1', { text: 'x' }); await api.cancelDelegation('r1', 'grant_1');
  await api.proposals('r1'); await api.proposal('r1', 'prp_1'); await api.decideProposal('r1', 'prp_1', { decision: 'accept', expected_digest: 'd' });
  assert.deepEqual(calls.map((c) => c.slice(0, 2)), [
    ['GET', '/api/v1/collab/rooms/r1/delegations'], ['POST', '/api/v1/collab/rooms/r1/delegations'],
    ['POST', '/api/v1/collab/rooms/r1/tasks/grant_1/cancel'], ['GET', '/api/v1/collab/rooms/r1/proposals'],
    ['GET', '/api/v1/collab/rooms/r1/proposals/prp_1'], ['POST', '/api/v1/collab/rooms/r1/proposals/prp_1/decide']]);
  assert.ok(!Object.getOwnPropertyNames(CollabApi.prototype).some((n) => /apply|execute|run/i.test(n)));
});

test('collaboration administration encodes scope and keeps removal distinct from permanent deletion', async () => {
  for (const access of ['public-token', 'operator']) {
    const calls = [];
    const api = new CollabApi(() => ({ access }), async (url, init) => {
      calls.push([init.method, url, JSON.parse(init.body)]);
      return response({ status: 'complete' });
    });
    assert.equal(typeof api.deletePerson, 'function');
    await api.removeMember('r/1', 'u/1');
    await api.deletePerson('u/1');
    await api.approveMember('r/1', 'u/1');
    await api.setUploadPermission('r/1', 'u/1', false);
    await api.archiveRoom('r/1');
    assert.deepEqual(calls, [
      ['POST', '/api/v1/collab/rooms/r%2F1/members/u%2F1/remove', {}],
      ['POST', '/api/v1/collab/admin/users/u%2F1/delete', {}],
      ['POST', '/api/v1/collab/rooms/r%2F1/members/u%2F1/approve', {}],
      ['POST', '/api/v1/collab/rooms/r%2F1/members/u%2F1/upload-permission', { file_upload_permission: false }],
      ['POST', '/api/v1/collab/rooms/r%2F1/archive', {}],
    ]);
  }
});

test('collaboration administration renderers separate pending, removed and upload capabilities', async () => {
  const view = await import('../collab-view.js');
  assert.equal(typeof view.renderPeople, 'function');
  assert.equal(typeof view.renderRoomAdministration, 'function');
  const users = [{ user_id: 'a', email: '<Admin>', status: 'pending' }];
  assert.doesNotMatch(view.renderPeople({ users }), /Delete person permanently/);
  assert.match(view.renderPeople({ users, manage: true }), /Delete person permanently/);
  assert.match(view.renderPeople({ users, manage: true }), /&lt;Admin&gt;/);
  const html = view.renderRoomAdministration({ room: { name: '<Lab>', members: [
    { user_id: 'owner', role: 'owner', email: 'owner', file_upload_permission: true },
    { user_id: 'pending', role: 'contributor', email: 'pending', room_membership_status: 'pending' },
    { user_id: 'member', role: 'contributor', email: '<Member>', room_membership_status: 'approved', file_upload_permission: false },
    { user_id: 'removed', role: 'viewer', email: 'removed', room_membership_status: 'removed' },
  ] }, canManage: true, invites: [] });
  assert.match(html, /Approve member/);
  assert.match(html, /Remove from room/);
  assert.match(html, /Upload files/);
  assert.match(html, /&lt;Member&gt;/);
  assert.doesNotMatch(html, /data-rm="owner"|data-upload="pending"|data-rm="removed"/);
  assert.doesNotMatch(view.renderRoomAdministration({ room: {}, canManage: false }), /data-upload|data-rm|Archive and delete/);
});

test('collaboration destructive confirmations explain retention and fail closed without a dialog', async (t) => {
  const view = await import('../collab-view.js');
  assert.equal(typeof view.confirmDeletePerson, 'function');
  const prompts = [];
  const previous = globalThis.confirm;
  t.after(() => { if (previous === undefined) delete globalThis.confirm; else globalThis.confirm = previous; });
  globalThis.confirm = (message) => { prompts.push(message); return false; };
  assert.equal(view.confirmDeletePerson({ email: 'member@example.org' }), false);
  assert.equal(view.confirmDeleteRoom({ name: 'Lab' }), false);
  assert.match(prompts[0], /memberships.*sessions.*tokens.*invitations.*owned.agent.*authored messages/is);
  assert.match(prompts[0], /archives/i);
  assert.match(prompts[0], /room-owned files.*remain/i);
  assert.match(prompts[1], /verif/i);
  assert.match(prompts[1], /failed archive.*live room intact/i);
  delete globalThis.confirm;
  assert.equal(view.confirmDeletePerson({ user_id: 'member' }), false);
  assert.equal(view.confirmDeleteRoom({ name: 'Lab' }), false);
});
