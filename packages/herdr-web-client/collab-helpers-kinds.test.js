import test from 'node:test';
import assert from 'node:assert/strict';
import { createHelpersPanel, renderEgressRequests, renderHostsTable, wireHelperHandlers } from '../collab-helpers.js';

const XSS = '<img src=x onerror=alert(1)>';
const fakeContainer = () => {
  const l = { click: new Set(), submit: new Set() };
  return { l, innerHTML: '', addEventListener: (t, f) => l[t].add(f), removeEventListener: (t, f) => l[t].delete(f), fire(t, ev) { for (const f of [...l[t]]) f(ev); } };
};
const tick = () => new Promise((r) => setImmediate(r));
const REQS = [{ id: 'a1', room_id: 'r5', host_port: 'tmp.org:443', state: 'approved', expires_at: 4e9 }];

test('entries kinds: Always / Until / Model endpoint, and no Remove on model', () => {
  const entries = [{ host_port: 'p.org:443', kind: 'permanent' }, { host_port: 'tmp.org:443', kind: 'approved' }, { host_port: 'api.anthropic.com:443', kind: 'model' }];
  const h = renderHostsTable(['ignored:1'], REQS, { operator: true, room: 'r5', entries, now: 1000 });
  assert.doesNotMatch(h, /ignored:1/);
  const row = (hp) => h.split('<tr ').find((x) => x.includes(`data-hp="${hp}"`));
  assert.match(row('p.org:443'), /Always/); assert.match(row('p.org:443'), /data-egress-act="remove"/);
  assert.match(row('tmp.org:443'), /Until /); assert.match(row('tmp.org:443'), /data-egress-act="remove"/);
  assert.match(row('api.anthropic.com:443'), /Model endpoint/); assert.doesNotMatch(row('api.anthropic.com:443'), /remove/);
});

test('entries: approved without a known live approval still renders, host escaped', () => {
  const h = renderHostsTable([], [], { operator: true, room: 'r', entries: [{ host_port: `${XSS}:1`, kind: 'approved' }] });
  assert.match(h, /Until it expires/); assert.doesNotMatch(h, /<img/);
});

test('no entries: falls back to the old allowlist rendering', () => {
  const h = renderHostsTable(['x.example:443'], [], { operator: true, room: 'r', permanent: ['x.example:443'] });
  assert.match(h, /Always/); assert.match(h, /remove/);
  const unknown = renderHostsTable(['x.example:443'], [], { operator: true, room: 'r', permanent: null });
  assert.match(unknown, /Allowed/);
});

test('internal requests show a warning badge and confirm-gate approve/always, not deny', () => {
  const h = renderEgressRequests([{ id: 'g', room_id: 'r', agent_id: 'a', host_port: '10.0.0.5:80', state: 'open', internal: true },
    { id: 'h', room_id: 'r', agent_id: 'a', host_port: 'pub.org:443', state: 'open', internal: false }], { operator: true });
  const [, internal, plain] = h.split('<li ');
  assert.match(internal, /private\/internal address/);
  for (const a of ['approve-1d', 'approve-1w', 'always']) assert.match(internal, new RegExp(`data-egress-act="${a}" data-confirm="`));
  assert.doesNotMatch(internal, /data-egress-act="deny" data-confirm/);
  assert.doesNotMatch(plain, /internal address|data-confirm/);
});

test('confirm gating: declined does nothing, accepted proceeds, deny never asks, no dialog fails closed', async () => {
  const calls = []; const asked = [];
  const api = { decideEgress: (...a) => { calls.push(a); return Promise.resolve(); }, promoteEgress: (...a) => { calls.push(['promote', ...a]); return Promise.resolve(); } };
  const mk = (confirm) => {
    const c = fakeContainer();
    wireHelperHandlers(c, { api, roomId: null, perRow: true, operator: true, owner: false, act: (fn) => fn(), refresh: async () => {}, confirm });
    return c;
  };
  const click = (ds) => ({ target: { closest: () => ({ dataset: ds, closest: () => ({ dataset: { id: 'g', room: 'r', hp: '10.0.0.5:80' } }) }) } });
  const no = mk((m) => { asked.push(m); return false; });
  no.fire('click', click({ egressAct: 'approve-1d', confirm: 'sure?' })); await tick();
  no.fire('click', click({ egressAct: 'always', confirm: 'sure?' })); await tick();
  assert.deepEqual(calls, []); assert.deepEqual(asked, ['sure?', 'sure?']);
  no.fire('click', click({ egressAct: 'deny' })); await tick();
  assert.deepEqual(calls, [['g', 'deny']]); assert.equal(asked.length, 2);
  const yes = mk(() => true);
  yes.fire('click', click({ egressAct: 'approve-1w', confirm: 'sure?' })); await tick();
  assert.deepEqual(calls.at(-1), ['g', 'approve', 604800]);
  const n = calls.length;
  mk().fire('click', click({ egressAct: 'approve-1d', confirm: 'sure?' })); await tick();   // node has no global confirm
  assert.equal(calls.length, n);
});

test('panel fetches the union of rooms_with_hosts, request rooms and own rooms (capped), and uses entries', async () => {
  const fetched = [];
  const own = Array.from({ length: 70 }, (_, i) => ({ room_id: `own${i}`, name: `o${i}` }));
  const api = {
    egressRequests: async () => ({ requests: [{ id: 'q', room_id: 'rq', agent_id: 'a', host_port: '192.168.1.1:80', state: 'open', internal: true }], rooms_with_hosts: ['rh', 'rq'] }),
    helpers: async () => ({ paused: false, helpers: [] }),
    egressAllowlist: async (r) => { fetched.push(r); return r === 'rh' ? { allowlist: ['m:1', 'p:1'], entries: [{ host_port: 'm:1', kind: 'model' }, { host_port: 'p:1', kind: 'permanent' }] } : { allowlist: [] }; },
  };
  const container = fakeContainer();
  const panel = createHelpersPanel({ container, api, operator: true, getRooms: async () => own, go() {}, schedule: () => 0, clear() {} });
  await panel.refresh();
  assert.deepEqual(fetched.slice(0, 2), ['rh', 'rq']);
  assert.equal(new Set(fetched).size, fetched.length); assert.equal(fetched.length, 60);
  assert.match(container.innerHTML, /Model endpoint/); assert.match(container.innerHTML, /private\/internal address/);
  const modelRow = container.innerHTML.split('<tr ').find((x) => x.includes('data-hp="m:1"'));
  assert.doesNotMatch(modelRow, /remove/);
});

test('panel falls back to allowlist when entries is absent', async () => {
  const api = { egressRequests: async () => ({ requests: [] }), helpers: async () => ({ helpers: [] }), egressAllowlist: async () => ({ allowlist: ['old.org:443'] }) };
  const container = fakeContainer();
  const panel = createHelpersPanel({ container, api, operator: true, getRooms: async () => [{ room_id: 'r1', name: 'R' }], go() {}, schedule: () => 0, clear() {} });
  await panel.refresh();
  assert.match(container.innerHTML, /old\.org:443/); assert.match(container.innerHTML, /Allowed/);
});
