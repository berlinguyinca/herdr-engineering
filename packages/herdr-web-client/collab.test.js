import test from 'node:test';
import assert from 'node:assert/strict';
import { pkcePair, authorizeUrl, parseRedirect, exchangeCode, TokenStore, tokenExpiry, CollabApi, AuthError } from './collab.js';

const cfg = { client_id: 'web', hosted_ui_domain: 'https://auth.example/' };
const jwt = (exp) => `h.${btoa(JSON.stringify({ exp })).replaceAll('+', '-').replaceAll('/', '_')}.s`;

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
