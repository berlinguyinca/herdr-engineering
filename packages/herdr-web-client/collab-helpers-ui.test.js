import test from 'node:test';
import assert from 'node:assert/strict';
import { CollabApi } from './collab.js';
import {
  helperAction, parseProfileForm, renderEgressRequests, renderHelperTree, renderHostsTable,
  renderPanic, renderProfileEditor, wireHelperHandlers,
} from '../collab-helpers.js';

const XSS = '<img src=x onerror=alert(1)>';
const PROFILE = {
  allowed_families: ['claude-code'], allowed_models: ['m1'], tools_allowed: true,
  permanent_hosts: ['pypi.org:443'], services: [{ name: 'gh', upstream_url: 'https://api.github.com', header: 'Authorization', secret_name: 'GH_TOKEN' }],
  ssh_secret_names: ['deploy_key'], spend_tokens_per_day: 5, limits: { live_per_agent: 2, depth: 1, per_hour: 3 },
};
const REQS = [
  { id: 'g1', room_id: 'r1', agent_id: 'a1', host_port: 'evil.example:443', state: 'open' },
  { id: 'g2', room_id: 'r1', agent_id: 'a1', host_port: 'ok.example:443', state: 'approved', expires_at: 2000000000 },
  { id: 'g3', room_id: 'r1', agent_id: 'a1', host_port: 'no.example:443', state: 'denied' },
];

function fakeApi() {
  const calls = [];
  const rec = (n) => (...a) => { calls.push([n, ...a]); return Promise.resolve({}); };
  return { calls, decideEgress: rec('decideEgress'), promoteEgress: rec('promoteEgress'), removeEgress: rec('removeEgress'),
    setRoomProfile: rec('setRoomProfile'), pauseHelpers: rec('pauseHelpers') };
}

test('CollabApi wrappers use the server routes (profile is written with POST)', async () => {
  const seen = [];
  const api = new CollabApi(() => ({ access: 't' }), async (url, o) => { seen.push([o.method, url, o.body ? JSON.parse(o.body) : null]); return { ok: true, status: 200, json: async () => ({}) }; });
  await api.roomProfile('r/1'); await api.setRoomProfile('r1', { tools_allowed: true });
  await api.egressRequests(); await api.decideEgress('g1', 'approve', 86400); await api.decideEgress('g1', 'deny');
  await api.promoteEgress('r1', 'a.org:443'); await api.removeEgress('r1', 'a.org:443');
  await api.egressAllowlist('r1'); await api.helpers(); await api.pauseHelpers(true);
  assert.deepEqual(seen, [
    ['GET', '/api/v1/collab/rooms/r%2F1/profile', null],
    ['POST', '/api/v1/collab/rooms/r1/profile', { tools_allowed: true }],
    ['GET', '/api/v1/collab/fleet/egress', null],
    ['POST', '/api/v1/collab/fleet/egress/g1/decide', { decision: 'approve', ttl_seconds: 86400 }],
    ['POST', '/api/v1/collab/fleet/egress/g1/decide', { decision: 'deny' }],
    ['POST', '/api/v1/collab/fleet/egress/promote', { room_id: 'r1', host_port: 'a.org:443' }],
    ['POST', '/api/v1/collab/fleet/egress/remove', { room_id: 'r1', host_port: 'a.org:443' }],
    ['GET', '/api/v1/collab/fleet/egress/r1', null],
    ['GET', '/api/v1/collab/fleet/helpers', null],
    ['POST', '/api/v1/collab/fleet/helpers/pause', { paused: true }],
  ]);
});

test('request buttons call the right wrappers with the right arguments', async () => {
  const api = fakeApi();
  const ds = { egressAct: '', id: 'g1', room: 'r1', hp: 'evil.example:443' };
  for (const act of ['approve-1d', 'approve-1w', 'deny', 'always', 'remove']) await helperAction(api, { ...ds, egressAct: act });
  assert.deepEqual(api.calls, [
    ['decideEgress', 'g1', 'approve', 86400],
    ['decideEgress', 'g1', 'approve', 604800],
    ['decideEgress', 'g1', 'deny'],
    ['promoteEgress', 'r1', 'evil.example:443'],
    ['decideEgress', 'g1', 'approve', 86400],
    ['removeEgress', 'r1', 'evil.example:443'],
  ]);
  const html = renderEgressRequests(REQS, { operator: true });
  for (const act of ['approve-1d', 'approve-1w', 'always', 'deny']) assert.match(html, new RegExp(`data-egress-act="${act}"`));
  assert.match(html, /Approve 1 day/); assert.match(html, /Approve 1 week/);
  assert.equal((html.match(/data-egress-act="deny"/g) || []).length, 1, 'only open requests get buttons');
});

test('the panic switch calls pauseHelpers with the opposite of its state', async () => {
  const api = fakeApi();
  await helperAction(api, { panic: 'pause' });
  await helperAction(api, { panic: 'resume' });
  assert.deepEqual(api.calls, [['pauseHelpers', true], ['pauseHelpers', false]]);
});

test('a non-operator sees no approve, always, deny or remove buttons', () => {
  const html = renderEgressRequests(REQS, { operator: false }) + renderHostsTable(PROFILE.permanent_hosts, REQS, { operator: false }) + renderPanic(true, { operator: false });
  assert.doesNotMatch(html, /data-egress-act|data-panic|<button/);
  assert.equal(renderEgressRequests(REQS, { operator: false }), '');
});

test('a non-owner sees no profile editor; the owner does, and it never asks for secret values', () => {
  assert.equal(renderProfileEditor(PROFILE, { owner: false }), '');
  const html = renderProfileEditor(PROFILE, { owner: true });
  assert.match(html, /<form/); assert.match(html, /GH_TOKEN/); assert.match(html, /deploy_key/);
  assert.match(html, /host CLI/i);
  assert.doesNotMatch(html, /type="password"|name="(secret|value|password|token)"/i);
});

test('the profile form round-trips names and keeps fields it does not edit', () => {
  const body = parseProfileForm({
    allowed_models: 'm1, m2', tools_allowed: false, permanent_hosts: 'a.org:443\n\n b.org:8443 ',
    services: 'gh | https://api.github.com | Authorization | GH_TOKEN', ssh_secret_names: 'k1\nk2',
  }, PROFILE);
  assert.deepEqual(body.allowed_models, ['m1', 'm2']);
  assert.equal(body.tools_allowed, false);
  assert.deepEqual(body.permanent_hosts, ['a.org:443', 'b.org:8443']);
  assert.deepEqual(body.services, [{ name: 'gh', upstream_url: 'https://api.github.com', header: 'Authorization', secret_name: 'GH_TOKEN' }]);
  assert.deepEqual(body.ssh_secret_names, ['k1', 'k2']);
  assert.deepEqual(body.limits, PROFILE.limits); assert.equal(body.spend_tokens_per_day, 5);
  assert.deepEqual(parseProfileForm({ allowed_models: '' }, PROFILE).allowed_models, null);
  assert.throws(() => parseProfileForm({ services: 'only | three | fields' }, PROFILE), /service/i);
  const saved = fakeApi();
  return helperAction(saved, { profile: 'save', room: 'r1' }, { profileBody: body }).then(() => assert.deepEqual(saved.calls, [['setRoomProfile', 'r1', body]]));
});

test('panic switch shows its state', () => {
  assert.match(renderPanic(true, { operator: true }), /Helpers paused/);
  assert.match(renderPanic(true, { operator: true }), /data-panic="resume"/);
  assert.match(renderPanic(false, { operator: true }), /Helpers running/);
  assert.match(renderPanic(false, { operator: true }), /data-panic="pause"/);
});

test('the hosts table lists permanent and live approved hosts with Remove for the operator', () => {
  const html = renderHostsTable(['pypi.org:443'], REQS, { operator: true, now: 1000, room: 'r1' });
  assert.match(html, /data-room="r1"/); assert.match(html, /pypi\.org:443/); assert.match(html, /ok\.example:443/);
  assert.doesNotMatch(html, /evil\.example|no\.example/);
  assert.equal((html.match(/data-egress-act="remove"/g) || []).length, 2);
});

test('the helper tree nests helpers under their parent agent', () => {
  const flow = { nodes: [
    { id: 'a:p', kind: 'agent', agent_id: 'p', name: 'lead' },
    { id: 'a:h', kind: 'agent', agent_id: 'h', name: 'herd-lead-1', parent_agent_id: 'p' },
    { id: 'a:g', kind: 'agent', agent_id: 'g', name: 'herd-lead-2', parent_agent_id: 'h' },
  ] };
  const html = renderHelperTree('p', flow, [{ helper_agent_id: 'h', status: 'claimed', task: 'build it' }], { operator: true });
  assert.match(html, /herd-lead-1/); assert.match(html, /build it/);
  assert.ok(html.indexOf('herd-lead-1') < html.indexOf('herd-lead-2'));
  assert.match(html, /<ul[^>]*>[\s\S]*<ul/);
  assert.equal(renderHelperTree('g', flow, [], {}), '');
});

test('host, task, name and secret-name strings are escaped everywhere', () => {
  const reqs = [{ id: 'g"1', room_id: 'r1', agent_id: XSS, host_port: XSS, state: 'open' },
    { id: 'g2', room_id: 'r1', agent_id: 'a', host_port: `${XSS}:1`, state: 'approved', expires_at: 2000000000 }];
  const flow = { nodes: [{ id: 'a:p', kind: 'agent', agent_id: 'p', name: 'lead' },
    { id: 'a:h', kind: 'agent', agent_id: 'h', name: XSS, parent_agent_id: 'p' }] };
  const prof = { ...PROFILE, permanent_hosts: [XSS], ssh_secret_names: [XSS], allowed_models: [XSS],
    services: [{ name: XSS, upstream_url: XSS, header: XSS, secret_name: XSS }] };
  const all = renderEgressRequests(reqs, { operator: true }) + renderHostsTable([XSS], reqs, { operator: true, now: 1 })
    + renderHelperTree('p', flow, [{ helper_agent_id: 'h', status: XSS, task: XSS }], { operator: true })
    + renderProfileEditor(prof, { owner: true });
  assert.doesNotMatch(all, /<img/);
  assert.match(all, /&lt;img src=x/);
});

function fakeContainer() {
  const l = { click: new Set(), submit: new Set() };
  return { l, addEventListener: (t, f) => l[t].add(f), removeEventListener: (t, f) => l[t].delete(f),
    fire(t, ev) { for (const f of [...l[t]]) f(ev); } };
}
const btn = (ds) => ({ target: { closest: () => ({ dataset: ds, closest: () => ({ dataset: { id: 'g1', hp: 'h.org:443' } }) }) } });

test('after visiting room A then B, a click in B only touches B (A is unbound on teardown)', async () => {
  const root = fakeContainer();
  const api = fakeApi();
  const mk = (roomId) => wireHelperHandlers(root, { api, roomId, operator: true, owner: true, act: (fn) => fn(), refresh: async () => {},
    getProfile: () => ({}), setProfile: () => {}, onError: () => {}, readForm: () => ({}) });
  const offA = mk('A'); offA();                      // room A's teardown runs when B opens
  mk('B');
  root.fire('click', btn({ egressAct: 'always' }));
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(api.calls.filter((c) => c[0] === 'promoteEgress'), [['promoteEgress', 'B', 'h.org:443']]);
  root.fire('submit', { target: { id: 'hcn-profile' }, preventDefault() {} });
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(api.calls.filter((c) => c[0] === 'setRoomProfile').map((c) => c[1]), ['B']);
  assert.equal(root.l.click.size, 1);
});

test('the operator hosts table shows the allowlist: Always after promote, Until for approvals, model endpoints fixed', () => {
  const reqs = [{ id: 'g1', room_id: 'r1', agent_id: 'a', host_port: 'new.org:443', state: 'approved', expires_at: 2000000000 }];
  const html = renderHostsTable(['api.anthropic.com:443', 'new.org:443', 'tmp.org:443'], [...reqs, { ...reqs[0], host_port: 'tmp.org:443' }], { operator: true, room: 'r1', now: 1000, permanent: ['new.org:443'] });
  assert.match(html, /new\.org:443<\/td><td>Always/); assert.match(html, /tmp\.org:443<\/td><td>Until/);
  assert.match(html, /Model endpoint/);
  assert.equal((html.match(/data-egress-act="remove"/g) || []).length, 2);
});

test('the profile editor has a family checkbox group that is saved', () => {
  const html = renderProfileEditor({ ...PROFILE, allowed_families: ['codex'] }, { owner: true });
  for (const f of ['claude-code', 'codex', 'pi', 'agent']) assert.match(html, new RegExp(`name="allowed_families" value="${f}"`));
  assert.match(html, /value="codex" checked/); assert.doesNotMatch(html, /value="pi" checked/);
  assert.deepEqual(parseProfileForm({ allowed_families: ['pi', 'codex'] }, PROFILE).allowed_families, ['pi', 'codex']);
  assert.deepEqual(parseProfileForm({ allowed_families: [] }, PROFILE).allowed_families, []);
});

test('Always still reports success when the follow-up decide is refused (409)', async () => {
  const api = fakeApi();
  api.decideEgress = () => Promise.reject(new Error('already decided'));
  await helperAction(api, { egressAct: 'always', id: 'g1', room: 'r1', hp: 'h.org:443' });
  assert.deepEqual(api.calls, [['promoteEgress', 'r1', 'h.org:443']]);
});
