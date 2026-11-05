import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { MemoryAuth, dispatchAuthCallback } from './memory-auth.js';
import { HerdrClient, ApiError } from './client.js';
import { TokenStore } from './collab.js';
const origin = 'https://private.example:9447';
const cfg = { enabled: true, origin, client_id: 'test-client', hosted_ui_domain: 'https://login.example', redirect_uri: `${origin}/` };
const jwt = `x.${Buffer.from(JSON.stringify({ exp: Date.now() / 1000 + 3600 })).toString('base64url')}.x`;
function fixture(href = `${origin}/#/memory`, config = cfg) {
  const values = new Map();
  const storage = { getItem: k => values.get(k) ?? null, setItem: (k,v) => values.set(k,v), removeItem: k => values.delete(k) };
  const location = new URL(href); location.assign = url => { location.assigned = url; };
  const calls = [], replacements = [];
  const fetchImpl = async (url, options) => { calls.push({url, options}); return new Response(JSON.stringify(String(url).endsWith('/oauth2/token') ? { access_token: jwt, id_token: jwt } : config)); };
  const auth = new MemoryAuth({ storage, location, history: { replaceState: (_,__,url) => replacements.push(url) }, fetchImpl, cryptoImpl: webcrypto });
  return { auth, storage, location, calls, replacements };
}
test('PKCE callback exchanges once, strips query and isolates collaboration tokens', async () => {
  const f = fixture(); new TokenStore(f.storage).set({access:jwt,id:'collab'});
  await f.auth.beginSignIn();
  const target = new URL(f.location.assigned), state = target.searchParams.get('state');
  assert.match(state, /^memory\./); assert.equal(target.searchParams.get('redirect_uri'), `${origin}/`);
  assert.equal(target.searchParams.get('code_challenge_method'), 'S256');
  f.location.search = `?code=secret&state=${state}`;
  assert.equal(await f.auth.completeCallback(), true);
  assert.equal(f.replacements.at(-1), '/#/memory');
  assert.equal(f.auth.tokens.get().access, jwt);
  assert.equal(new TokenStore(f.storage).get().id, 'collab');
  const exchange = f.calls.find(c => c.url.endsWith('/oauth2/token'));
  assert.equal(exchange.options.redirect, 'error');
  assert.equal(exchange.options.body.get('redirect_uri'), `${origin}/`);
  f.auth.signOut(); assert.equal(f.auth.tokens.get(), null); assert.ok(new TokenStore(f.storage).get());
});
test('only HTTPS same-origin Memory GET receives bearer; native keys survive', async () => {
  const f = fixture(); await f.auth.config(); f.auth.tokens.set({access:jwt,id:jwt});
  for (const [base,path,method,want] of [
    ['', '/api/v1/memory/search?q=x','GET',true], ['', '/api/v1/memory/status','GET',true],
    ['', '/api/v1/memory/id','GET',true], ['', '/api/v1/missions','GET',false],
    ['', '/api/v1/memory/search','POST',false], ['https://evil.example','/api/v1/memory/status','GET',false],
    ['http://private.example:9447','/api/v1/memory/status','GET',false],
    ['', '/api/v1/memory/../missions','GET',false], ['', '/api/v1/memory/auth/config','GET',false],
  ]) {
    let sent;
    const client = new HerdrClient(base, {memoryAuth:f.auth, fetchImpl: async (url,options) => {sent=options; return new Response('{}');}});
    await client.request(path,{method});
    assert.equal(sent.headers.Authorization, want ? `Bearer ${jwt}` : undefined, `${base}${path} ${method}`);
    assert.equal(sent.headers['X-API-Key'],undefined); assert.equal(sent.headers.Origin,undefined);
    if (want) assert.equal(sent.redirect,'error');
  }
  f.location.protocol='http:';
  let sent; await new HerdrClient('',{memoryAuth:f.auth,fetchImpl:async (_,o)=>{sent=o;return new Response('{}');}}).getMemoryServiceStatus();
  assert.equal(sent.headers.Authorization,undefined);
});
test('Memory failures retain structured ApiError and reject redirects', async () => {
  const f=fixture(); await f.auth.config(); f.auth.tokens.set({access:jwt,id:jwt});
  const client=new HerdrClient('',{memoryAuth:f.auth,fetchImpl:async (_,opts)=> {assert.equal(opts.redirect,'error');return new Response(JSON.stringify({error:'no access',code:'denied'}),{status:403});}});
  await assert.rejects(client.getMemoryServiceStatus(),e=>e instanceof ApiError && e.body.code==='denied' && e.message==='no access');
});
test('invalid configuration origins and redirects never navigate or exchange', async () => {
  for (const bad of ['null','http://private.example:9447','https://private.example:9447/','https://private.example:9447?x','https://private.example:9447#x','https://u@private.example:9447','https://private.example:bad','https://[private]:9447','https://private.example:65536','https://private.example:9447/path',' https://private.example:9447','https://private.example:9447 ','https://private.example:9447\n','https://PRIVATE.example:9447','https://private.example:09447']) {
    const f=fixture(undefined,{...cfg,origin:bad}); await assert.rejects(f.auth.beginSignIn()); assert.equal(f.location.assigned,undefined);
  }
  for (const redirect_uri of [`${origin}/other`,`${origin}/?x`,`${origin}/#x`]) {const f=fixture(undefined,{...cfg,redirect_uri});await assert.rejects(f.auth.beginSignIn());}
});
test('HTTP and optional unavailable config preserve native operation without tokens', async () => {
  const f=fixture('http://dashboard.example/#/memory'); assert.equal(await f.auth.config(),null); assert.equal(f.calls.length,0); await assert.rejects(f.auth.beginSignIn(),/HTTPS/);
  const g=fixture();g.auth.fetch=async()=>new Response('{}',{status:404});assert.equal(await g.auth.config(),null);
});
test('bad, missing, duplicate, expired, foreign-origin callbacks are consumed before collab and scrubbed', async () => {
  for (const mode of ['mismatch','missing','duplicate','expired','future','origin','redirect','error','storage']) {
    const f=fixture(); await f.auth.beginSignIn(); const state=new URL(f.location.assigned).searchParams.get('state');
    f.location.search=mode==='missing'?'?code=secret':mode==='error'?`?error=denied&state=${state}`:`?code=secret&state=${mode==='mismatch'?'memory.wrong':state}${mode==='duplicate'?'&code=second':''}`;
    const pending=JSON.parse(f.storage.getItem('herdr.memory.pkce'));
    if(mode==='expired')pending.createdAt=Date.now()-700000;
    if(mode==='future')pending.createdAt=Date.now()+60000;
    if(mode==='origin')pending.origin='https://evil.example';
    if(mode==='redirect')f.location.pathname='/other';
    f.storage.setItem('herdr.memory.pkce',mode==='storage'?'broken':JSON.stringify(pending));
    let collab=0; await dispatchAuthCallback(f.auth,async()=>{collab++;});
    assert.equal(collab,0,mode); assert.equal(f.auth.tokens.get(),null,mode);assert.equal(f.replacements.at(-1),'/#/memory',mode);
    assert.equal(f.calls.filter(c=>c.url.endsWith('/oauth2/token')).length,0,mode);
  }
});
test('collab callbacks remain dispatchable; malformed Memory state is never delegated',async()=>{
  const f=fixture();f.location.search='?code=c&state=collab-state';let calls=0;await dispatchAuthCallback(f.auth,async()=>calls++);assert.equal(calls,1);
  f.location.search='?code=c&state=memory.bad';await dispatchAuthCallback(f.auth,async()=>calls++);assert.equal(calls,1);assert.equal(f.replacements.at(-1),'/#/memory');
});
test('a pending or stale Memory transaction leaves a valid collaboration callback untouched', async () => {
  for (const stale of [false, true]) {
    const f = fixture();
    await f.auth.beginSignIn();
    if (stale) {
      const saved = JSON.parse(f.storage.getItem('herdr.memory.pkce'));
      saved.createdAt = Date.now() - 700000;
      f.storage.setItem('herdr.memory.pkce', JSON.stringify(saved));
    }
    const pending = f.storage.getItem('herdr.memory.pkce');
    f.location.search = '?code=collab-code&state=hcn.foo';
    let delegated = 0;
    await dispatchAuthCallback(f.auth, async () => {
      delegated++;
      assert.equal(f.location.search, '?code=collab-code&state=hcn.foo');
    });
    assert.equal(delegated, 1);
    assert.equal(f.storage.getItem('herdr.memory.pkce'), pending);
    assert.deepEqual(f.replacements, []);
    assert.equal(f.calls.filter(c => c.url.endsWith('/oauth2/token')).length, 0);
    assert.equal(f.auth.tokens.get(), null);
  }
});
test('an explicit native API key wins over browser sign-in without changing client headers', async () => {
  const f = fixture(); await f.auth.config(); f.auth.tokens.set({ access: jwt, id: jwt });
  for (const keyName of ['X-API-Key', 'x-api-key']) {
    const sent = [];
    const client = new HerdrClient('', { memoryAuth: f.auth, headers: { [keyName]: 'native' },
      fetchImpl: async (_, options) => { sent.push(options); return new Response('{}'); } });
    await client.getMemoryServiceStatus();
    assert.equal(sent[0].headers.Authorization, undefined);
    assert.equal(sent[0].headers[keyName], 'native');
    assert.equal(client.headers[keyName], 'native');
  }
});
test('noncanonical configured default and padded ports fail before PKCE navigation', async () => {
  for (const bad of ['https://private.example:443', 'https://private.example:09447']) {
    const f = fixture(`${bad}/#/memory`, { ...cfg, origin: bad, redirect_uri: `${bad}/` });
    await assert.rejects(f.auth.beginSignIn());
    assert.equal(f.location.assigned, undefined);
    assert.equal(f.storage.getItem('herdr.memory.pkce'), null);
  }
});
