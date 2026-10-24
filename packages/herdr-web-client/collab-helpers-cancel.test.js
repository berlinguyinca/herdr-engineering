import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { CollabApi } from './collab.js';
import { createHelpersPanel, helperAction, renderHelperList, renderHelperTree, renderHostsTable, wireHelperHandlers } from '../collab-helpers.js';

const XSS = '<img src=x onerror=alert(1)>';
const V6 = '[2606:4700:4700::1111]:443';
const live = (o = {}) => ({ helper_id: 'h1', status: 'running', owner: 'u1', helper_agent_id: 'ha', role: 'r', room_id: 'r1', ...o });
const flow = { nodes: [{ kind: 'agent', agent_id: 'p', name: 'p' }, { kind: 'agent', agent_id: 'ha', name: 'kid', parent_agent_id: 'p' }] };

test('CollabApi.cancelHelper posts to helpers/{id}/cancel with the id encoded', async () => {
  const seen = [];
  const api = Object.create(CollabApi.prototype);
  api.call = (m, p, b) => { seen.push([m, p, b]); return Promise.resolve({}); };
  await api.cancelHelper('a/b'); await api.myHelpers();
  assert.deepEqual(seen, [['POST', '/helpers/a%2Fb/cancel', {}], ['GET', '/helpers', undefined]]);
});

test('Cancel button in the operator list calls cancelHelper with the helper id; hidden for finished helpers', async () => {
  const html = renderHelperList([live(), live({ helper_id: 'h2', status: 'done' })], [], 2000);
  assert.match(html, /data-cancel-helper="h1"/); assert.doesNotMatch(html, /data-cancel-helper="h2"/);
  const calls = [];
  await helperAction({ cancelHelper: (id) => { calls.push(id); return Promise.resolve(); } }, { cancelHelper: 'h1' });
  assert.deepEqual(calls, ['h1']);
});

test('tree: Cancel for operator or owner only, live only, no permission no button', () => {
  const t = (h, o) => renderHelperTree('p', flow, [h], o);
  assert.match(t(live(), { operator: true }), /data-cancel-helper="h1"/);
  assert.match(t(live(), { viewerId: 'u1' }), /data-cancel-helper="h1"/);
  assert.doesNotMatch(t(live(), { viewerId: 'u2' }), /data-cancel/);
  assert.doesNotMatch(t(live(), {}), /data-cancel/);
  assert.doesNotMatch(t(live({ status: 'cancelled' }), { operator: true }), /data-cancel/);
});

test('XSS: helper ids, roles and tasks are escaped around the button', () => {
  const h = live({ helper_id: `h"${XSS}`, role: XSS, task: XSS });
  for (const html of [renderHelperList([h], [], 2000), renderHelperTree('p', flow, [h], { operator: true })]) {
    assert.doesNotMatch(html, /<img/); assert.doesNotMatch(html, /data-cancel-helper="h"</);
  }
});

test('wired click: a non-operator owner can Cancel but not use operator buttons', async () => {
  let handler; const calls = [];
  const container = { addEventListener: (t, f) => { if (t === 'click') handler = f; }, removeEventListener() {} };
  wireHelperHandlers(container, { api: { cancelHelper: (id) => { calls.push(['cancel', id]); return Promise.resolve(); }, pauseHelpers: () => calls.push(['pause']) },
    roomId: 'r1', operator: false, owner: false, act: (fn) => fn(), refresh: async () => {} });
  const ev = (ds) => ({ target: { closest: () => ({ dataset: ds, closest: () => null }) } });
  handler(ev({ cancelHelper: 'h9' })); handler(ev({ panic: 'pause' }));
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(calls, [['cancel', 'h9']]);
});

test('IPv6 host:port renders unchanged and round-trips into Remove / promote payloads', async () => {
  const html = renderHostsTable([V6], [], { operator: true, room: 'r1', permanent: [V6] });
  assert.ok(html.includes(`data-hp="${V6}"`));
  const calls = [];
  const api = { removeEgress: (...a) => calls.push(['rm', ...a]), promoteEgress: (...a) => { calls.push(['pr', ...a]); return Promise.resolve(); }, decideEgress: () => Promise.resolve() };
  await helperAction(api, { egressAct: 'remove', room: 'r1', hp: V6 });
  await helperAction(api, { egressAct: 'always', room: 'r1', hp: V6, id: 'g' });
  assert.deepEqual(calls, [['rm', 'r1', V6], ['pr', 'r1', V6]]);
});

test('no hardcoded model-endpoint list: kind is derived from the profile, never from host names', () => {
  const src = readFileSync(new URL('../collab-helpers.js', import.meta.url), 'utf8');
  assert.doesNotMatch(src, /anthropic|openai|metabolomics|MODEL_ENDPOINTS/);
  // profile known: a host that is neither permanent nor an approval is the server's own endpoint
  const known = renderHostsTable(['x.example:443', 'p.org:443'], [], { operator: true, room: 'r', permanent: ['p.org:443'] });
  assert.match(known, /data-hp="x.example:443"[^]*?Model endpoint/); assert.match(known, /data-hp="p.org:443"[^]*?Always/);
  // profile unknown (guest/agent shape, no permanent_hosts): nothing is labelled a model endpoint
  for (const permanent of [null, undefined]) {
    const h = renderHostsTable(['x.example:443'], [], { operator: true, room: 'r', permanent });
    assert.doesNotMatch(h, /Model endpoint/); assert.match(h, /Allowed/); assert.match(h, /data-egress-act="remove"/);
  }
});

test('operator panel passes no permanent list and survives rows without owner or allowed_families', async () => {
  const container = { innerHTML: '', addEventListener() {}, removeEventListener() {} };
  const api = { egressRequests: async () => ({ requests: [] }), helpers: async () => ({ helpers: [live({ owner: undefined })] }),
    egressAllowlist: async () => ({ allowlist: [V6] }) };
  const panel = createHelpersPanel({ container, api, operator: true, getRooms: async () => [{ room_id: 'r1', name: 'R' }], schedule: () => 0, clear() {} });
  await panel.refresh();
  assert.ok(container.innerHTML.includes(`data-hp="${V6}"`)); assert.doesNotMatch(container.innerHTML, /Model endpoint/);
});

test('the operator cancels through the fleet route, an owner through the person route', async () => {
  const calls = [];
  const api = { cancelHelper: (id) => calls.push(['own', id]), fleetCancelHelper: (id) => calls.push(['fleet', id]) };
  await helperAction(api, { cancelHelper: 'h1' }, { operator: true });
  await helperAction(api, { cancelHelper: 'h2' });
  assert.deepEqual(calls, [['fleet', 'h1'], ['own', 'h2']]);
});

test('CollabApi.fleetCancelHelper posts to fleet/helpers/{id}/cancel', async () => {
  const seen = [];
  const api = new CollabApi(() => ({ access: 't' }), async (url, init) => { seen.push([init.method, url]); return { ok: true, status: 200, json: async () => ({}) }; });
  await api.fleetCancelHelper('a/b');
  assert.deepEqual(seen, [['POST', '/api/v1/collab/fleet/helpers/a%2Fb/cancel']]);
});
