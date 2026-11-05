// The library client against a real local HTTP server (no fetch stub): paths, bodies and tickets of the folder and pending calls.
import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createMemberApi, createSessionStore } from './member-api.js';

const future = () => Math.floor(Date.now() / 1000) + 3600;

async function serve(handler) {
  const seen = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      seen.push({ method: req.method, url: req.url, headers: req.headers, body: Buffer.concat(chunks).toString() });
      handler(req, res);
    });
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  return { base: `http://127.0.0.1:${server.address().port}`, seen, close: () => new Promise((r) => server.close(r)) };
}

async function withApi(handler, fn) {
  const srv = await serve(handler);
  const sessions = createSessionStore({ getItem: () => null, setItem() {}, removeItem() {} });
  sessions.set({ session: 'S', expires_at: future() });
  const api = createMemberApi({ sessions, refresh: async () => {}, onSignedOut() {}, base: srv.base, sleep: async () => {} });
  try { await fn(api, srv.seen); } finally { await srv.close(); }
}

const reply = (res, status, doc, type = 'application/json') => { res.writeHead(status, { 'Content-Type': type }); res.end(typeof doc === 'string' ? doc : JSON.stringify(doc)); };

test('library() reads items and folders; every write is a ticket polled to its result', async () => {
  await withApi((req, res) => {
    if (req.url === '/api/member/library') return reply(res, 200, { items: [{ artifact_id: 'a' }], folders: [{ folder_id: 'f' }] });
    if (req.url.startsWith('/api/member/results/')) return reply(res, 200, { status: 'done', result: 'ok' });
    return reply(res, 200, { status: 'pending', ticket: 't1' });
  }, async (api, seen) => {
    assert.deepEqual(await api.library(), { items: [{ artifact_id: 'a' }], folders: [{ folder_id: 'f' }] });
    await api.createFolder('Topic');
    await api.renameFolder('fld_1', 'New');
    await api.deleteFolder('fld_1');
    await api.fileItem('fld_1', 'art-1');
    await api.unfileItem('fld_1', 'art-1');
    await api.acceptPending('art-1');
    await api.discardPending('art-1');
    const sent = seen.filter((s) => !s.url.startsWith('/api/member/results/'));
    const calls = sent.map((s) => `${s.method} ${s.url}`);
    assert.deepEqual(calls, [
      'GET /api/member/library', 'POST /api/member/folders', 'PUT /api/member/folders/fld_1', 'DELETE /api/member/folders/fld_1',
      'POST /api/member/folders/fld_1/items', 'DELETE /api/member/folders/fld_1/items/art-1',
      'POST /api/member/pending/art-1/accept', 'POST /api/member/pending/art-1/discard',
    ]);
    assert.deepEqual(JSON.parse(sent[1].body), { name: 'Topic' });
    assert.deepEqual(JSON.parse(sent[2].body), { name: 'New' });
    assert.deepEqual(JSON.parse(sent.find((s) => s.url.endsWith('/items') && s.method === 'POST').body), { artifact_id: 'art-1' });
    assert.ok(seen.every((s) => s.headers.authorization === 'Member S'));
  });
});

test('contentUrl percent-encodes the path segments and fetchContent sends the member session, never a token', async () => {
  await withApi((req, res) => reply(res, 200, '<p>hi</p>', 'text/html'), async (api, seen) => {
    assert.equal(api.contentUrl('art-1', 'dir/a b.html'), '/api/member/artifacts/art-1/content/dir/a%20b.html');
    const got = await api.fetchContent('art-1', 'dir/a b.html');
    assert.equal(got.type, 'text/html');
    assert.equal(await got.blob.text(), '<p>hi</p>');
    assert.equal(seen[0].url, '/api/member/artifacts/art-1/content/dir/a%20b.html');
    assert.equal(seen[0].headers.authorization, 'Member S');
  });
});
