import test from 'node:test';
import assert from 'node:assert/strict';
import { edgeState, layoutFlow } from '../collab-flow.js';
import * as collab from './collab.js';

test('collab routes share page, room ID and status across public and dashboard surfaces', () => {
  assert.equal(typeof collab.collabRouteState, 'function');
  for (const surface of ['public', 'dashboard']) {
    assert.deepEqual(collab.collabRouteState({ surface, route: '#/room/r%2F1', status: 'loading' }),
      { surface, route: '/room', roomId: 'r/1', status: 'loading' });
    assert.deepEqual(collab.collabRouteState({ surface, route: '/agents', status: 'ready' }),
      { surface, route: '/agents', roomId: null, status: 'ready' });
    assert.deepEqual(collab.collabRouteState({ surface, route: '/collab' }),
      { surface, route: '/rooms', roomId: null, status: 'idle' });
    assert.deepEqual(collab.collabRouteState({ surface, route: '/room/%broken' }),
      { surface, route: '/rooms', roomId: null, status: 'not-found' });
    for (const [code, status] of [['unauthorized', 'signed-out'], ['forbidden', 'forbidden'],
      ['not_found', 'not-found'], ['unavailable', 'degraded'], ['network_error', 'degraded'],
      ['invalid_response', 'degraded'], ['rate_limited', 'degraded'], ['conflict', 'error']]) {
      assert.equal(collab.collabRouteState({ surface, error: { code } }).status, status);
    }
  }
});

// Minimal DOM boundary for the existing room-list shell. API, auth, normalization,
// mounting and routing are real; no browser dependency is needed for this flow.
test('collab shells load the same identity and do not leak operator auth between mounts', async (t) => {
  const storage = new Map();
  const sessionStorage = { getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) };
  const token = `h.${btoa(JSON.stringify({ exp: Date.now() / 1000 + 600 }))}.s`;
  const elements = new Map();
  const document = { querySelector: () => ({}), querySelectorAll: () => [], getElementById: (id) => elements.get(id) || null };
  const window = { sessionStorage, location: { hash: '#/rooms', origin: 'https://collab.example' },
    addEventListener() {}, removeEventListener() {} };
  t.mock.method(globalThis, 'fetch', async () => { throw new Error('fetch fixture not set'); });
  const oldWindow = globalThis.window, oldDocument = globalThis.document;
  const views = [];
  globalThis.window = window; globalThis.document = document;
  t.after(() => {
    for (const view of views) view.dispose();
    if (oldWindow === undefined) delete globalThis.window; else globalThis.window = oldWindow;
    if (oldDocument === undefined) delete globalThis.document; else globalThis.document = oldDocument;
  });
  const { mountCollab } = await import('../collab-view.js');
  for (const surface of ['dashboard', 'public']) for (const scenario of [
    { status: 'ready', http: 200, userStatus: 'approved', text: /No rooms yet/ },
    { status: 'ready', http: 200, userStatus: 'approved', text: /No rooms yet/, operatorConfig: true },
    { status: 'waiting', http: 200, userStatus: 'pending', text: /Waiting for approval/ },
    { status: 'forbidden', http: 403, userStatus: 'approved', text: /server failure/ },
    { status: 'not-found', http: 404, userStatus: 'approved', text: /server failure/ },
    { status: 'degraded', http: 503, userStatus: 'approved', text: /server failure/ },
    { status: 'signed-out', http: 401, userStatus: 'approved', text: surface === 'public' ? /Sign in/ : /session expired/ },
  ]) {
    sessionStorage.setItem('hcn.tokens', JSON.stringify({ access: token, id: 'id-token' }));
    for (const id of ['hcn-new', 'hcn-out', 'hcn-out2', 'hcn-out3', 'hcn-signin']) elements.set(id, {});
    const calls = [];
    globalThis.fetch = async (url, init) => {
      calls.push([url, init?.headers?.Authorization]);
      const cfg = { client_id: 'web', hosted_ui_domain: 'https://auth.example', public_url: 'https://collab.example',
        ...(surface === 'dashboard' || scenario.operatorConfig ? { operator: { user_id: 'u1' } } : {}) };
      const status = url.endsWith('/rooms') ? scenario.http : 200;
      const data = status !== 200 ? { error: 'server failure' } : url.endsWith('/config') ? cfg : url.endsWith('/me')
        ? { user_id: 'u1', user_status: scenario.userStatus } : { rooms: [] };
      return { ok: status === 200, status, json: async () => data };
    };
    const root = { ownerDocument: document, dataset: { standalone: surface === 'public' ? '1' : '0' },
      innerHTML: '', querySelectorAll: () => [] };
    const view = mountCollab(root);
    views.push(view);
    await new Promise((resolve) => setImmediate(resolve));
    assert.ok(view.ready instanceof Promise, 'mount exposes completion for both shells');
    await view.ready;
    assert.deepEqual(view.state, { surface, route: '/rooms', roomId: null, status: scenario.status });
    assert.match(root.innerHTML, scenario.text);
    if (scenario.status === 'waiting') assert.ok(calls.every(([url]) => !url.endsWith('/rooms')));
    assert.ok(calls.some(([url]) => url.endsWith('/me')), 'dashboard also uses the shared identity endpoint');
    assert.ok(calls.filter(([url]) => !url.endsWith('/config')).every(([, auth]) =>
      auth === `Bearer ${surface === 'dashboard' || scenario.operatorConfig ? 'operator' : token}`));
    view.dispose();
  }
});

const flow = {
  nodes: [
    { id: 'u:alice', kind: 'person', name: 'alice' },
    { id: 'a:1', kind: 'agent', name: 'lead', family: 'claude-code' },
    { id: 'a:2', kind: 'agent', name: 'coder', family: 'codex' },
    { id: 'a:3', kind: 'agent', name: 'idle', family: 'pi' },
  ],
  edges: [
    { from: 'u:alice', to: 'a:1', counts: { accepted: 1 }, last: 't' },
    { from: 'a:1', to: 'a:2', counts: { pending: 2, done: 1 }, last: 't' },
  ],
};

test('a delegation chain reads left to right and idle agents wait in a grid to the right', () => {
  const lay = layoutFlow(flow);
  const x = Object.fromEntries(lay.nodes.map((n) => [n.id, n.x]));
  assert.ok(x['u:alice'] < x['a:1'] && x['a:1'] < x['a:2']);
  assert.ok(x['a:3'] > x['a:2']);
  assert.equal(lay.idleLabel.text, 'Idle · 1');
  assert.equal(lay.edges.length, 2);
  assert.ok(lay.edges.every((e) => e.d.startsWith('M')));
});

test('edge state follows open work, then failures', () => {
  assert.deepEqual(edgeState({ pending: 1, done: 3 }), { state: 'pending', live: true, total: 4 });
  assert.equal(edgeState({ accepted: 1 }).state, 'accepted');
  assert.equal(edgeState({ failed: 1, done: 2 }).state, 'failed');
  assert.equal(edgeState({ done: 2 }).live, false);
});

test('dozens of idle agents wrap into short columns instead of one tall stack', () => {
  const many = { nodes: [{ id: 'u:a', kind: 'person', name: 'a' }], edges: [{ from: 'u:a', to: 'a:ghost', counts: { done: 1 }, last: '' }] };
  for (let i = 0; i < 60; i += 1) many.nodes.push({ id: `a:${i}`, kind: 'agent', name: `w${i}`, family: 'codex' });
  const lay = layoutFlow(many);
  assert.equal(lay.nodes.length, 61);
  assert.equal(lay.edges.length, 0);                    // edges to unknown nodes are dropped
  assert.ok(lay.height <= 8 * 46 + 80, `height ${lay.height}`);
  assert.ok(lay.width < 2000);
  assert.equal(lay.idleLabel.text, 'Idle · 60');
  assert.equal(new Set(lay.nodes.map((n) => `${n.x},${n.y}`)).size, 61);   // nobody overlaps
});

test('an agent started under a parent sits one column right of it, is not idle, and gets a dashed spawn edge', () => {
  const lay = layoutFlow({
    nodes: [{ id: 'a:p', kind: 'agent', name: 'lead', family: 'claude-code' }, { id: 'a:c', kind: 'agent', name: 'helper-1', family: 'claude-code', group: 'herd-helper' }],
    edges: [], spawns: [{ from: 'a:p', to: 'a:c' }],
  });
  const x = Object.fromEntries(lay.nodes.map((n) => [n.id, n.x]));
  assert.equal(lay.idleLabel, null);
  assert.ok(x['a:p'] < x['a:c']);
  assert.deepEqual(lay.edges.map((e) => [e.key, e.state, e.spawn]), [['a:p>a:c#spawn', 'spawn', true]]);
});
