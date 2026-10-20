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

test('getCi requests /api/v1/ci', async () => {
  const calls = [];
  const client = new HerdrClient('', {
    fetchImpl: async (url) => { calls.push(url); return { ok: true, text: async () => '{"available": false}' }; },
  });
  const r = await client.getCi();
  assert.equal(calls[0], '/api/v1/ci');
  assert.equal(r.available, false);
});

test('getTests requests /api/v1/tests', async () => {
  const calls = [];
  const client = new HerdrClient('', {
    fetchImpl: async (url) => { calls.push(url); return { ok: true, text: async () => '{"adapters": ["pytest"]}' }; },
  });
  const r = await client.getTests();
  assert.equal(calls[0], '/api/v1/tests');
  assert.deepEqual(r.adapters, ['pytest']);
});

test('mission activity GETs encode exact namespaces and forward cancellation', async () => {
  const calls = [];
  const client = new HerdrClient('', { fetchImpl: async (url, opts) => {
    calls.push({url, opts}); return {ok: true, text: async () => '{}'};
  }});
  const signal = new AbortController().signal;
  await client.listMissionActivity({signal});
  await client.missionActivity('pi:MSN/1', {signal});
  assert.equal(calls[0].url, '/api/v1/mission-activity');
  assert.equal(calls[1].url, '/api/v1/missions/pi%3AMSN%2F1/activity');
  assert.ok(calls.every(c => c.opts.method === 'GET' && c.opts.signal === signal));
});

test('listMissions preserves status filtering while forwarding cancellation', async () => {
 const controller=new AbortController();let request;
 const client=new HerdrClient('',{fetchImpl:async(url,opts)=>{request={url,opts};return{ok:true,status:200,text:async()=>'{}'};}});
 await client.listMissions('in progress',{signal:controller.signal});
 assert.equal(request.url,'/api/v1/missions?status=in%20progress');assert.equal(request.opts.signal,controller.signal);
});

test('dashboard reads preserve filters and forward cancellation signals', async () => {
  const calls=[]; const controller=new AbortController();
  const client=new HerdrClient('', {fetchImpl: async (url,opts)=>{
    calls.push({url,opts}); return {ok:true,status:200,text:async()=> '{}'};
  }});
  await client.listSessions('active',{signal:controller.signal});
  await client.listServices({signal:controller.signal});
  await client.listHosts({signal:controller.signal});
  await client.health({signal:controller.signal});
  await client.activity(8,{signal:controller.signal});
  assert.deepEqual(calls.map(x=>x.url), ['/api/v1/sessions?status=active','/api/v1/services','/api/v1/hosts','/api/v1/health','/api/v1/activity?limit=8']);
  assert.ok(calls.every(x=>x.opts.signal===controller.signal));
});

test('session detail reads encode the exact source ID and support cancellation',async()=>{
 const controller=new AbortController();let call;
 const client=new HerdrClient('',{fetchImpl:async(url,opts)=>{call={url,opts};return{ok:true,text:async()=> '{}'};}});
 const id='herdr:beast:%2Fwork%2Frepo:w1:p3';
 await client.getSession(id,{signal:controller.signal});
 assert.equal(call.url,`/api/v1/sessions/${encodeURIComponent(id)}`);
 assert.equal(call.opts.signal,controller.signal);
});
