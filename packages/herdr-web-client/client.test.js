import test from 'node:test';
import assert from 'node:assert/strict';

import { HerdrClient, ApiError } from './client.js';
import { connectEvents } from './events.js';

function fakeFetch(routes) {
  return async (url, opts) => {
    const hit = routes.find((r) => url.endsWith(r.path));
    if (!hit) {
      return {
        ok: false, status: 404,
        text: async () => JSON.stringify({ error: 'not found' }),
      };
    }
    return {
      ok: true, status: 200,
      text: async () => JSON.stringify(hit.body),
    };
  };
}

test('listMissions requests the v1 endpoint and parses JSON', async () => {
  const client = new HerdrClient('http://x', {
    fetchImpl: fakeFetch([
      { path: '/api/v1/missions', body: { missions: [{ id: 'm1' }] } },
    ]),
  });
  const res = await client.listMissions();
  assert.deepEqual(res, { missions: [{ id: 'm1' }] });
});

test('sendMessage posts message + artifact_ids as JSON', async () => {
  let sent = null;
  const client = new HerdrClient('', {
    fetchImpl: async (url, opts) => {
      sent = { url, opts, body: JSON.parse(opts.body) };
      return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true }) };
    },
  });
  await client.sendMessage('s1', 'hello', ['a1', 'a2']);
  assert.ok(sent.url.endsWith('/api/v1/sessions/s1/messages'));
  assert.equal(sent.opts.method, 'POST');
  assert.deepEqual(sent.body, { message: 'hello', artifact_ids: ['a1', 'a2'] });
});

test('ApiError carries status and body on non-2xx', async () => {
  const client = new HerdrClient('', {
    fetchImpl: async () => ({
      ok: false, status: 403,
      text: async () => JSON.stringify({ error: 'forbidden' }),
    }),
  });
  await assert.rejects(() => client.listMissions(), (err) => {
    assert.ok(err instanceof ApiError);
    assert.equal(err.status, 403);
    assert.equal(err.message, 'forbidden');
    return true;
  });
});

test('calls fetch detached so native Window.fetch does not throw', async () => {
  // Simulates a browser where fetch is `this`-sensitive: calling it as a
  // method (receiver != undefined) throws "Illegal invocation".
  function browserFetch(url, opts) {
    if (this !== undefined) {
      throw new TypeError("Failed to execute 'fetch' on 'Window': Illegal invocation");
    }
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true }) };
  }
  const client = new HerdrClient('', { fetchImpl: browserFetch });
  const res = await client.listMissions();
  assert.deepEqual(res, { ok: true });
});


test('connectEvents dispatches parsed events and disconnects cleanly', () => {
  const seen = [];
  let handler = null;
  const fakeES = {
    onmessage: null, onerror: null,
    close() { fakeES.closed = true; },
  };
  const disconnect = connectEvents({
    url: '/events',
    onEvent: (e) => seen.push(e),
    createEventSource: () => fakeES,
    autoReconnectMs: 0,
  });
  fakeES.onmessage({ data: '{"event_type":"MissionCreated"}' });
  fakeES.onmessage({ data: 'not-json' }); // ignored, no crash
  assert.deepEqual(seen, [{ event_type: 'MissionCreated' }]);
  disconnect();
  assert.equal(fakeES.closed, true);
});
