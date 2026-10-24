import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';

import { HerdrClient, ApiError } from './client.js';
import { connectEvents } from './events.js';

test('agent summary and rename requests preserve encoded identity and version', async () => {
  const calls = [];
  const client = new HerdrClient('', { fetchImpl: async (url, opts) => {
    calls.push({ url, method: opts.method, body: opts.body && JSON.parse(opts.body) });
    return { ok: true, status: 200, text: async () => '{}' };
  } });
  await client.getAgentSummary('a:b');
  await client.regenerateAgentSummary('a:b');
  await client.renameAgent('a:b', 'Ada', 2);
  await client.fixSummaryItem('a:b', 'blockers', 1, 'CI is red');
  assert.deepEqual(calls.map(c => c.url), ['/api/v1/sessions/a%3Ab/summary', '/api/v1/sessions/a%3Ab/summary', '/api/v1/sessions/a%3Ab/name', '/api/v1/sessions/a%3Ab/summary/fix']);
  assert.equal(calls[1].method, 'POST');
  assert.deepEqual(calls[2].body, { name: 'Ada', expected_version: 2 });
  assert.equal(calls[3].method, 'POST');
  assert.deepEqual(calls[3].body, { list: 'blockers', index: 1, text: 'CI is red' });
});

test('debugCi sends mode only for a fix request', async () => {
  const bodies = [];
  const client = new HerdrClient('', { fetchImpl: async (url, opts) => {
    bodies.push(JSON.parse(opts.body));
    return { ok: true, status: 200, text: async () => '{}' };
  } });
  await client.debugCi('17', 12, 's', 'op1');
  await client.debugCi('17', 12, 's', 'op2', { mode: 'fix', providerUrl: 'https://ci.example' });
  await client.debugCi('17', 12, 's', 'op3', { mode: 'other' });
  assert.deepEqual(bodies[0], { repository: '17', number: 12, session_id: 's', operation_id: 'op1' });
  assert.deepEqual(bodies[1], { repository: '17', number: 12, session_id: 's', operation_id: 'op2', provider_url: 'https://ci.example', mode: 'fix' });
  assert.equal('mode' in bodies[2], false);
});

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

test('sendMessage retains an explicit operation identity across retries', async () => {
  const bodies=[];
  const client=new HerdrClient('',{fetchImpl:async(url,options)=>{
    bodies.push(JSON.parse(options.body));return {ok:true,status:200,text:async()=>'{"status":"delivered"}'};
  }});
  await client.sendMessage('herdr:h:default:w1%3Ap1','hello',[],'message-1');
  await client.sendMessage('herdr:h:default:w1%3Ap1','hello',[],'message-1');
  assert.deepEqual(bodies.map(b=>b.operation_id),['message-1','message-1']);
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

test('CI detail, logs and review reads encode identities and forward cancellation',async()=>{
  const calls=[];
  const client=new HerdrClient('',{fetchImpl:async(url,opts)=>{calls.push({url,opts});return {ok:true,status:200,text:async()=>'{}'};}});
  const signal=new AbortController().signal;
  await client.getCi({signal});await client.getCiPipeline('owner/repo & other',12,{signal});
  await client.getCiLog('owner/repo',12,'step/9',{signal});await client.listReviews({signal});
  assert.equal(calls[0].url,'/api/v1/ci');
  assert.equal(new URL(calls[1].url,'http://test').searchParams.get('repository'),'owner/repo & other');
  assert.equal(new URL(calls[2].url,'http://test').searchParams.get('step'),'step/9');
  assert.equal(calls[3].url,'/api/v1/reviews');
  for(const call of calls)assert.equal(call.opts.signal,signal);
});
test('CI mutation uses explicit action POST with the provider run number',async()=>{
  let sent;
  const client=new HerdrClient('',{fetchImpl:async(url,opts)=>{sent={url,opts};return {ok:true,status:200,text:async()=>'{}'};}});
  await client.ciAction('owner/repo',42,'retry');
  assert.equal(sent.url,'/api/v1/ci/actions');assert.equal(sent.opts.method,'POST');
  assert.deepEqual(JSON.parse(sent.opts.body),{repository:'owner/repo',number:42,action:'retry'});
});

test('GitHub inventory reads encode filters and pass cancellation without mutations', async () => {
  const calls = [];
  const client = new HerdrClient('', { fetchImpl: async (url, opts) => {
    calls.push({ url, opts });
    return { ok: true, status: 200, text: async () => '{}' };
  }});
  const signal = new AbortController().signal;
  await client.getGithub({ signal });
  await client.getGithub({ page: 2, per_page: 25, search: 'a&b / c', signal });
  await client.getGithubIssues('github.com/owner/repo', {
    tab: 'blocked', search: 'status = broken & needs love', page: 3, per_page: 20, signal,
  });
  await client.getGithubIssues('', { signal });
  await client.getGithubIssue('github.com/owner/repo', 42, { signal });
  assert.equal(calls[0].url, '/api/v1/github');
  assert.deepEqual(Object.fromEntries(new URL(calls[1].url, 'http://test').searchParams), {
    page: '2', per_page: '25', search: 'a&b / c',
  });
  assert.deepEqual(Object.fromEntries(new URL(calls[2].url, 'http://test').searchParams), {
    repository: 'github.com/owner/repo', tab: 'blocked', search: 'status = broken & needs love',
    page: '3', per_page: '20',
  });
  assert.equal(calls[3].url, '/api/v1/github/issues');
  assert.deepEqual(Object.fromEntries(new URL(calls[4].url, 'http://test').searchParams), {
    repository: 'github.com/owner/repo', number: '42',
  });
  for (const { opts } of calls) {
    assert.equal(opts.signal, signal);
    assert.equal(opts.method, 'GET');
    assert.equal(opts.body, undefined);
  }
});

test('GitHub failures retain the shared structured error contract', async () => {
  const client = new HerdrClient('', { fetchImpl: async () => ({
    ok: false, status: 503, text: async () => '{"error":"Integration service is unavailable."}',
  }) });
  await assert.rejects(client.getGithub(), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 503);
    assert.equal(error.message, 'Integration service is unavailable.');
    return true;
  });
});

test('CI report history reads use viewer GET and forward cancellation',async()=>{
 const calls=[];
 const server=createServer((req,res)=>{calls.push({url:req.url,method:req.method});res.setHeader('Content-Type','application/json');res.end(JSON.stringify({reports:[],pipelines:[]}));});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 try {
  const client=new HerdrClient(`http://127.0.0.1:${server.address().port}`);
  assert.deepEqual(await client.getCiReports(),{reports:[],pipelines:[]});
  assert.deepEqual(calls[0],{url:'/api/v1/ci/reports',method:'GET'});
  const controller=new AbortController();
  const request=client.getCiReports({signal:controller.signal});controller.abort();
  await assert.rejects(request,{name:'AbortError'});
 } finally {server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
});

test('CI heatmap history uses the viewer endpoint and supports cancellation',async()=>{
 const calls=[];
 const server=createServer((req,res)=>{calls.push({url:req.url,method:req.method});res.setHeader('Content-Type','application/json');res.end(JSON.stringify({repos:[],pipelines:[],coverage:{}}));});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 try {
  const client=new HerdrClient(`http://127.0.0.1:${server.address().port}`);
  assert.deepEqual(await client.getCiHistory(),{repos:[],pipelines:[],coverage:{}});
  assert.deepEqual(calls[0],{url:'/api/v1/ci/history',method:'GET'});
  const controller=new AbortController();
  const request=client.getCiHistory({signal:controller.signal});controller.abort();
  await assert.rejects(request,{name:'AbortError'});
 } finally {server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
});

test('CI performance uses bounded-window viewer GET filters and forwards cancellation',async()=>{
  const calls=[];const client=new HerdrClient('',{fetchImpl:async(url,options)=>{calls.push({url,options});return {ok:true,status:200,text:async()=>'{"cache_groups":[],"execution_trends":[],"alerts":[]}'};}});
  const controller=new AbortController();
  assert.deepEqual(await client.getCiPerformance({days:7,repository:'org/app',branch:'feature & cache',signal:controller.signal}),{cache_groups:[],execution_trends:[],alerts:[]});
  assert.equal(calls[0].url,'/api/v1/ci/performance?days=7&repository=org%2Fapp&branch=feature+%26+cache');
  assert.equal(calls[0].options.method,'GET');assert.equal(calls[0].options.signal,controller.signal);
  await client.getCiPerformance();assert.equal(calls[1].url,'/api/v1/ci/performance?days=14');
});

test('CI reads and mutations bind the expected provider URL without changing legacy requests',async()=>{
 const calls=[];
 const client=new HerdrClient('',{fetchImpl:async(url,opts)=>{calls.push({url,opts});return {ok:true,status:200,text:async()=>'{}'};}});
 const providerUrl='https://ci.example/secondary';
 await client.getCiPipeline('17',12,{providerUrl});
 await client.getCiLog('17',12,'4',{providerUrl});
 await client.ciAction('17',12,'retry',{providerUrl});
 await client.ciAction('17',12,'cancel',{providerUrl});
 await client.debugCi('17',12,'session','op',{providerUrl});
 for(const call of calls.slice(0,2))assert.equal(new URL(call.url,'http://test').searchParams.get('provider_url'),providerUrl);
 for(const call of calls.slice(2))assert.equal(JSON.parse(call.opts.body).provider_url,providerUrl);
});
