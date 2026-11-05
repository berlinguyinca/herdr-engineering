// member-api against a real local HTTP server (no fetch stub): session scheme, one re-exchange on 401, Retry-After, tickets.
import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createMemberApi, createSessionStore, ApiError, SESSION_KEY } from './member-api.js';

function memoryStorage() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), m };
}
const future = () => Math.floor(Date.now() / 1000) + 3600;

async function serve(handler) {
  const seen = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const body = Buffer.concat(chunks);
      seen.push({ method: req.method, url: req.url, headers: req.headers, body });
      handler(req, res, seen);
    });
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  return { base: `http://127.0.0.1:${server.address().port}`, seen, close: () => new Promise((r) => server.close(r)) };
}
const json = (res, status, doc, headers = {}) => { res.writeHead(status, { 'Content-Type': 'application/json', ...headers }); res.end(JSON.stringify(doc)); };
const noSleep = async () => {};

test('the session store keeps the session in memory and sessionStorage, expires it, and clears both', () => {
  const storage = memoryStorage();
  const a = createSessionStore(storage);
  assert.equal(a.get(), null);
  a.set({ session: 'tok', expires_at: future() });
  assert.equal(a.get(), 'tok');
  assert.ok(storage.m.has(SESSION_KEY));
  assert.equal(createSessionStore(storage).get(), 'tok');           // a reload reads it back
  a.clear();
  assert.equal(storage.m.size, 0);
  assert.equal(a.get(), null);
  storage.setItem(SESSION_KEY, JSON.stringify({ session: 'old', expires_at: 1 }));
  assert.equal(createSessionStore(storage).get(), null);             // expired
  storage.setItem(SESSION_KEY, '{not json');
  assert.equal(createSessionStore(storage).get(), null);
  const broken = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); }, removeItem() { throw new Error('denied'); } };
  const b = createSessionStore(broken);
  b.set({ session: 'mem', expires_at: future() });
  assert.equal(b.get(), 'mem');                                     // memory still works when storage throws
  b.clear();
});

test('requests carry Authorization: Member; a 401 re-exchanges once and retries once', async () => {
  let valid = 'old';
  const s = await serve((req, res) => {
    if (req.headers.authorization === `Member ${valid}`) json(res, 200, { ok: true });
    else json(res, 401, { error: 'unauthorized', message: 'sign in again' });
  });
  try {
    const sessions = createSessionStore(memoryStorage());
    sessions.set({ session: 'old', expires_at: future() });
    valid = 'new';
    let refreshed = 0;
    const api = createMemberApi({ base: s.base, sessions, refresh: async () => { refreshed += 1; sessions.set({ session: 'new', expires_at: future() }); }, onSignedOut() { throw new Error('should not sign out'); } });
    assert.deepEqual(await api.me(), { ok: true });
    assert.equal(refreshed, 1);
    assert.equal(s.seen.length, 2);
    assert.equal(s.seen[0].headers.authorization, 'Member old');
    assert.equal(s.seen[1].headers.authorization, 'Member new');
  } finally { await s.close(); }
});

test('a 401 that survives the re-exchange signs out instead of looping', async () => {
  const s = await serve((req, res) => json(res, 401, { error: 'unauthorized' }));
  try {
    const sessions = createSessionStore(memoryStorage());
    sessions.set({ session: 'x', expires_at: future() });
    let out = 0;
    const api = createMemberApi({ base: s.base, sessions, refresh: async () => {}, onSignedOut: () => { out += 1; } });
    await assert.rejects(api.me(), (e) => e instanceof ApiError && e.status === 401);
    assert.equal(s.seen.length, 2);                                  // first try plus exactly one retry
    assert.equal(out, 1);
    const failing = createMemberApi({ base: s.base, sessions, refresh: async () => { throw new Error('no tokens'); }, onSignedOut: () => { out += 1; } });
    await assert.rejects(failing.me(), (e) => e.status === 401);
    assert.equal(out, 2);
  } finally { await s.close(); }
});

test('no session at all goes straight to sign-in without a request', async () => {
  const s = await serve((req, res) => json(res, 200, {}));
  try {
    let out = 0;
    const api = createMemberApi({ base: s.base, sessions: createSessionStore(memoryStorage()), refresh: async () => {}, onSignedOut: () => { out += 1; } });
    await assert.rejects(api.me(), (e) => e.status === 401);
    assert.equal(out, 1);
    assert.equal(s.seen.length, 0);
  } finally { await s.close(); }
});

test('429 waits Retry-After and retries; a run PUT leaves the retry to the uploader and exposes retryAfter', async () => {
  let n = 0;
  const s = await serve((req, res) => {
    n += 1;
    if (req.method === 'GET' && n === 1) return json(res, 429, { error: 'rate_limited' }, { 'Retry-After': '7' });
    if (req.method === 'PUT') return json(res, 429, { error: 'rate_limited', message: 'slow down' }, { 'Retry-After': '3' });
    return json(res, 200, { links: [] });
  });
  try {
    const sessions = createSessionStore(memoryStorage());
    sessions.set({ session: 's', expires_at: future() });
    const waits = [];
    const api = createMemberApi({ base: s.base, sessions, refresh: async () => {}, sleep: async (ms) => { waits.push(ms); } });
    assert.deepEqual(await api.links(), { links: [] });
    assert.deepEqual(waits, [7000]);
    const meta = { name: 'a b/é.txt', size_bytes: 3, file_sha256: 'f'.repeat(64), run_sha256: 'e'.repeat(64) };
    await assert.rejects(api.putRun('a1', 'u'.repeat(8), 0, new Uint8Array([1, 2, 3]), meta), (e) => e.status === 429 && e.retryAfter === 3);
    const put = s.seen.at(-1);
    assert.equal(put.headers['x-name'], encodeURIComponent('a b/é.txt'));
    assert.equal(put.headers['x-size-bytes'], '3');
    assert.equal(put.headers['x-file-sha256'], 'f'.repeat(64));
    assert.deepEqual([...put.body], [1, 2, 3]);
    assert.match(put.url, /\/api\/member\/artifacts\/a1\/uploads\/u{8}\/runs\/0$/);
  } finally { await s.close(); }
});

test('writes poll their ticket until done; share events are a POST; delete confirms', async () => {
  let polls = 0;
  const s = await serve((req, res) => {
    if (req.method === 'GET' && req.url.startsWith('/api/member/results/')) {
      polls += 1;
      return json(res, 200, polls < 3 ? { ticket: 't1', status: 'pending' } : { ticket: 't1', status: 'done', result: 'created', artifact_id: 'art-1' });
    }
    return json(res, 200, { ticket: 't1', status: 'pending' });
  });
  try {
    const sessions = createSessionStore(memoryStorage());
    sessions.set({ session: 's', expires_at: future() });
    const api = createMemberApi({ base: s.base, sessions, refresh: async () => {}, sleep: noSleep });
    const done = await api.create('T', 'index.html', 'web');
    assert.equal(done.artifact_id, 'art-1');
    assert.equal(polls, 3);
    assert.match(s.seen[0].url, /^\/api\/member\/artifacts$/);
    const sent = JSON.parse(s.seen[0].body.toString());
    assert.equal(sent.title, 'T');
    assert.ok(sent.idempotency_key.length >= 8);
    polls = 0;
    await api.shareEvents('a/1', 's 1');
    const ev = s.seen.find((r) => r.url.includes('/events'));
    assert.equal(ev.method, 'POST');
    assert.equal(ev.url, '/api/member/artifacts/a%2F1/shares/s%201/events');
    await api.remove('a1');
    assert.deepEqual(JSON.parse(s.seen.find((r) => r.url.endsWith('/delete')).body.toString()), { confirm: true });
  } finally { await s.close(); }
});
