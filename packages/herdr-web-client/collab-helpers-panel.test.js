import test from 'node:test';
import assert from 'node:assert/strict';
import { createHelpersPanel, renderHelpersPanel } from '../collab-helpers.js';

const XSS = '<img src=x onerror=alert(1)>';
const ROOMS = [{ room_id: 'r1', name: 'Mine' }];
const REQS = [
  { id: 'g1', room_id: 'r1', agent_id: 'a1', host_port: 'one.org:443', state: 'open' },
  { id: 'g2', room_id: 'r2', agent_id: 'a2', host_port: `${XSS}:443`, state: 'open' },
  { id: 'g3', room_id: 'r2', agent_id: 'a2', host_port: 'done.org:443', state: 'approved', expires_at: 4e9 },
  { id: 'g4', room_id: 'r3', agent_id: 'a3', host_port: 'no.org:443', state: 'denied' },
];
const HELPERS = [{ helper_id: 'h1', room_id: 'r2', role: `rev${XSS}`, status: 'running', requester_agent_id: `ag${XSS}`, task: `t${XSS}`, created_epoch: 1000, expires_epoch: 5000 }];

function fakeContainer() {
  const l = { click: new Set(), submit: new Set() };
  return { l, innerHTML: '', addEventListener: (t, f) => l[t].add(f), removeEventListener: (t, f) => l[t].delete(f),
    fire(t, ev) { for (const f of [...l[t]]) f(ev); } };
}
function fakeApi({ paused = false } = {}) {
  const calls = [];
  const rec = (n, v = {}) => (...a) => { calls.push([n, ...a]); return Promise.resolve(v); };
  return { calls, egressRequests: rec('egressRequests', { requests: REQS }), helpers: rec('helpers', { paused, helpers: HELPERS }),
    egressAllowlist: (r) => { calls.push(['egressAllowlist', r]); return Promise.resolve({ allowlist: r === 'r2' ? ['done.org:443', 'api.anthropic.com:443'] : [] }); },
    decideEgress: rec('decideEgress'), promoteEgress: rec('promoteEgress'), removeEgress: rec('removeEgress'), pauseHelpers: rec('pauseHelpers') };
}
const click = (ds, rowDs) => ({ target: { closest: (sel) => (sel.includes('data-panic') ? { dataset: ds, closest: () => (rowDs ? { dataset: rowDs } : null) } : null) } });
const tick = () => new Promise((r) => setImmediate(r));
const mk = async (over = {}) => {
  const container = fakeContainer(); const api = over.api || fakeApi();
  const panel = createHelpersPanel({ container, api, operator: true, getRooms: async () => ROOMS, go: () => {}, schedule: () => 0, clear: () => {}, ...over });
  await panel.refresh();
  return { container, api, panel };
};

test('requests from ALL rooms render, with a link only for rooms the viewer can open', async () => {
  const { container } = await mk();
  const h = container.innerHTML;
  assert.match(h, /data-id="g1"[^>]*data-room="r1"/); assert.match(h, /data-id="g2"[^>]*data-room="r2"/);
  assert.match(h, /<a [^>]*data-go="\/room\/r1"[^>]*>Mine<\/a>/);
  assert.doesNotMatch(h, /data-go="\/room\/r2"/); assert.match(h, /\br2\b/);
  assert.doesNotMatch(h, /no\.org/);                          // denied is not open
});

test('buttons call the wrappers with the request own room', async () => {
  const { container, api } = await mk();
  const row = { id: 'g2', room: 'r2', hp: 'one.org:443' };
  container.fire('click', click({ egressAct: 'approve-1w' }, row)); await tick();
  container.fire('click', click({ egressAct: 'deny' }, row)); await tick();
  container.fire('click', click({ egressAct: 'always' }, row)); await tick();
  container.fire('click', click({ egressAct: 'remove' }, { room: 'r2', hp: 'done.org:443' })); await tick();
  const c = api.calls.filter((x) => /decide|promote|remove/i.test(x[0]));
  assert.deepEqual(c, [['decideEgress', 'g2', 'approve', 604800], ['decideEgress', 'g2', 'deny'],
    ['promoteEgress', 'r2', 'one.org:443'], ['decideEgress', 'g2', 'approve', 86400], ['removeEgress', 'r2', 'done.org:443']]);
});

test('panic switch shows its state and toggles', async () => {
  const on = await mk({ api: fakeApi({ paused: true }) });
  assert.match(on.container.innerHTML, /Helpers paused/); assert.match(on.container.innerHTML, /data-panic="resume"/);
  on.container.fire('click', click({ panic: 'resume' })); await tick();
  assert.deepEqual(on.api.calls.filter((x) => x[0] === 'pauseHelpers'), [['pauseHelpers', false]]);
  const off = await mk();
  assert.match(off.container.innerHTML, /Helpers running/);
});

test('live helpers list and per-room external hosts (only rooms with requests/hosts)', async () => {
  const { container, api } = await mk();
  assert.match(container.innerHTML, /running/); assert.match(container.innerHTML, /Helpers across rooms/);
  assert.doesNotMatch(container.innerHTML, /data-helper-cancel/);   // no server route to cancel a helper
  assert.match(container.innerHTML, /done\.org:443<\/td><td>Until/);
  assert.deepEqual(api.calls.filter((x) => x[0] === 'egressAllowlist').map((x) => x[1]).sort(), ['r1', 'r2']);   // r3 only has a denied request
});

test('non-operator gets no panel and makes no calls', async () => {
  const container = fakeContainer(); const api = fakeApi();
  const p = createHelpersPanel({ container, api, operator: false, getRooms: async () => ROOMS, go() {}, schedule: () => 0, clear() {} });
  await p.refresh();
  assert.equal(container.innerHTML, ''); assert.equal(api.calls.length, 0); assert.equal(container.l.click.size, 0);
  assert.equal(renderHelpersPanel({}, { operator: false }), '');
});

test('every dynamic string is escaped', async () => {
  const { container } = await mk({ getRooms: async () => [{ room_id: 'r2', name: XSS }] });
  assert.doesNotMatch(container.innerHTML, /<img/);
  assert.match(container.innerHTML, /&lt;img/);
  const err = await mk({ api: { ...fakeApi(), egressRequests: () => Promise.reject(new Error(XSS)) } });
  assert.doesNotMatch(err.container.innerHTML, /<img/); assert.match(err.container.innerHTML, /&lt;img/);
});

test('destroy unbinds, so a later click does nothing; a remount handles a click once', async () => {
  const container = fakeContainer(); const api = fakeApi();
  const o = { container, api, operator: true, getRooms: async () => ROOMS, go() {}, schedule: () => 0, clear() {} };
  const a = createHelpersPanel(o); await a.refresh(); a.destroy();
  assert.equal(container.l.click.size, 0);
  const b = createHelpersPanel(o); await b.refresh();
  container.fire('click', click({ egressAct: 'deny' }, { id: 'g1', room: 'r1' })); await tick();
  assert.equal(api.calls.filter((x) => x[0] === 'decideEgress').length, 1);
  b.destroy(); assert.equal(container.l.click.size, 0);
  await b.refresh();                                          // a late poll after destroy draws nothing
});

test('polling reschedules until destroyed', async () => {
  const timers = [];
  const { panel } = await mk({ schedule: (fn, ms) => { timers.push([fn, ms]); return timers.length; } });
  panel.start(); await tick();
  assert.equal(timers.length >= 1, true);
  panel.destroy(); const n = timers.length; await timers[timers.length - 1][0](); await tick();
  assert.equal(timers.length, n);
});
