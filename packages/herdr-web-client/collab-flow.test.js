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

const flush = () => new Promise((resolve) => setImmediate(resolve));
const jsonResponse = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

async function collabHarness(t, surface, userStatus = 'approved', intercept = () => null) {
  const { mountCollab } = await import('../collab-view.js');
  const storage = new Map();
  const sessionStorage = { getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) };
  const token = `h.${btoa(JSON.stringify({ exp: Date.now() / 1000 + 600 }))}.s`;
  sessionStorage.setItem('hcn.tokens', JSON.stringify({ access: token, id: 'id-token' }));
  const timers = new Map(), listeners = new Map(), calls = [];
  t.mock.method(globalThis, 'setTimeout', (fn, ms) => { const id = {}; timers.set(id, { fn, ms }); return id; });
  t.mock.method(globalThis, 'clearTimeout', (id) => timers.delete(id));
  const element = () => ({ dataset: {}, innerHTML: '', textContent: '', scrollHeight: 0,
    value: '', disabled: false, setAttribute() {}, reset() {},
    addEventListener() {}, removeEventListener() {}, querySelectorAll: () => [], contains: () => false });
  const elements = new Map(['hcn-new', 'hcn-out', 'hcn-out2', 'hcn-out3', 'hcn-signin', 'hcn-log', 'hcn-jump', 'hcn-connect', 'hcn-agents'].map((id) => [id, element()]));
  const links = ['/rooms', '/room/old', '/room/new', '/agents', '/people'].map((go) => ({ dataset: { go } }));
  const document = { hidden: true, querySelector: () => ({}), querySelectorAll: () => [],
    getElementById: (id) => elements.get(id) || null, addEventListener() {}, removeEventListener() {} };
  const window = { sessionStorage, location: { hash: '#/rooms', origin: 'https://collab.example' },
    addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: (name) => listeners.delete(name) };
  const oldWindow = globalThis.window, oldDocument = globalThis.document;
  globalThis.window = window; globalThis.document = document;
  let view;
  t.after(() => {
    view?.dispose();
    if (oldWindow === undefined) delete globalThis.window; else globalThis.window = oldWindow;
    if (oldDocument === undefined) delete globalThis.document; else globalThis.document = oldDocument;
  });
  const model = { userStatus, intercept };
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    const path = url.replace('/api/v1/collab', '');
    calls.push(path);
    const result = model.intercept(path, init);
    if (result) return result;
    const data = path === '/config' ? { client_id: 'web', hosted_ui_domain: 'https://auth.example',
      public_url: 'https://collab.example', ...(surface === 'dashboard' ? { operator: { user_id: 'u1' } } : {}) }
      : path === '/me' ? { user_id: 'u1', user_status: model.userStatus }
      : path === '/rooms' ? { rooms: [] }
      : path.startsWith('/rooms/') ? { room_id: path.slice(7), name: path.slice(7), role: 'guest' }
      : { agents: [] };
    return jsonResponse(data);
  });
  const root = { ...element(), ownerDocument: document, dataset: { standalone: surface === 'public' ? '1' : '0' },
    querySelectorAll: (selector) => selector === '[data-go]' ? links : [] };
  view = mountCollab(root);
  return { view, root, model, calls, storage, timers, elements, element, document, window,
    navigate: async (path) => {
      if (surface === 'public') { window.location.hash = `#${path}`; listeners.get('hashchange')?.(); }
      else links.find((link) => link.dataset.go === path).onclick();
      await flush();
    },
    poll: async () => {
      const entry = [...timers].find(([, timer]) => timer.ms === 5000);
      assert.ok(entry, 'approval polling is scheduled');
      timers.delete(entry[0]);
      await entry[1].fn();
      await flush();
    },
  };
}

for (const surface of ['public', 'dashboard']) {
  for (const stage of ['room', 'tabs', 'rooms', 'agents']) for (const outcome of ['success', 'error', 'auth', 'abort']) {
    for (const action of ['navigation', 'disposal']) test(`collab ${surface} ignores stale ${stage} ${outcome} after ${action}`, async (t) => {
      const h = await collabHarness(t, surface);
      await h.view.ready;
      const pending = deferred();
      let held = false;
      h.model.intercept = (path) => {
        if (!held && path === (stage === 'room' ? '/rooms/old' : stage === 'agents' ? '/agents' : '/rooms')) { held = true; return pending.promise; }
      };
      await h.navigate(stage === 'rooms' ? '/rooms' : stage === 'agents' ? '/agents' : '/room/old');
      assert.ok(held);
      if (action === 'navigation') await h.navigate('/room/new');
      else { h.view.dispose(); h.root.innerHTML = 'replacement view'; }
      const html = h.root.innerHTML, state = h.view.state, tokens = h.storage.get('hcn.tokens');
      const agentsHtml = h.elements.get('hcn-agents').innerHTML;
      if (outcome === 'abort') pending.reject(new DOMException('cancelled', 'AbortError'));
      else pending.resolve(outcome === 'success' ? jsonResponse(stage === 'room'
        ? { room_id: 'old', name: 'stale room', role: 'guest' } : stage === 'agents' ? { agents: [] } : { rooms: [] })
        : jsonResponse({ error: 'stale failure' }, outcome === 'auth' ? 401 : 503));
      await flush();
      assert.equal(h.root.innerHTML, html, 'late response must not render');
      assert.equal(h.elements.get('hcn-agents').innerHTML, agentsHtml, 'late nested render must not change content');
      assert.deepEqual(h.view.state, state, 'late response must not change current route status');
      assert.equal(h.storage.get('hcn.tokens'), tokens, 'late auth failure must not sign out current navigation');
      assert.equal(h.timers.size, 0, 'late responses must not start background work');
    });
  }

  for (const stage of ['/config', '/me']) for (const outcome of ['success', 'error']) {
    test(`collab ${surface} ignores startup ${stage} ${outcome} after disposal`, async (t) => {
      const pending = deferred();
      const h = await collabHarness(t, surface, 'pending', (path) => path === stage ? pending.promise : null);
      await flush();
      h.view.dispose();
      h.root.innerHTML = 'replacement view';
      const state = h.view.state;
      pending.resolve(outcome === 'error' ? jsonResponse({ error: 'late failure' }, 503)
        : jsonResponse(stage === '/me' ? { user_id: 'u1', user_status: 'pending' }
          : { client_id: 'web', hosted_ui_domain: 'https://auth.example', public_url: 'https://collab.example' }));
      await h.view.ready;
      assert.equal(h.root.innerHTML, 'replacement view');
      assert.deepEqual(h.view.state, state);
      assert.equal(h.timers.size, 0);
    });
  }

  test(`collab ${surface} ignores cancellation on the current route`, async (t) => {
    const h = await collabHarness(t, surface);
    await h.view.ready;
    const html = h.root.innerHTML;
    h.model.intercept = (path) => path === '/rooms/old' ? Promise.reject(new DOMException('cancelled', 'AbortError')) : null;
    await h.navigate('/room/old');
    assert.equal(h.root.innerHTML, html);
    assert.notEqual(h.view.state.status, 'error');
  });

  for (const [userStatus, status] of [['denied', 'forbidden'], ['disabled', 'revoked'], ['revoked', 'revoked'], ['kicked', 'revoked']]) {
    test(`collab ${surface} ${userStatus} is terminal instead of waiting`, async (t) => {
      const h = await collabHarness(t, surface, userStatus);
      await h.view.ready;
      assert.equal(h.view.state.status, status);
      assert.doesNotMatch(h.root.innerHTML, /Waiting for approval/);
      assert.equal(h.timers.size, 0);
      assert.ok(!h.calls.includes('/rooms'));
    });
  }

  test(`collab ${surface} pending approval becomes denied and stops polling`, async (t) => {
    const h = await collabHarness(t, surface, 'pending');
    await h.view.ready;
    h.model.userStatus = 'denied';
    await h.poll();
    assert.equal(h.view.state.status, 'forbidden');
    assert.doesNotMatch(h.root.innerHTML, /Waiting for approval/);
    assert.equal(h.timers.size, 0);
    assert.ok(!h.calls.includes('/rooms'));
  });

  test(`collab ${surface} approval polling exposes failure and recovers through pending to approved`, async (t) => {
    const h = await collabHarness(t, surface, 'pending');
    await h.view.ready;
    h.model.intercept = (path) => path === '/me' ? jsonResponse({ error: 'approval service offline' }, 503) : null;
    await h.poll();
    assert.equal(h.view.state.status, 'degraded');
    assert.match(h.root.innerHTML, /approval service offline/);
    assert.ok(!h.calls.includes('/rooms'));
    h.model.intercept = () => null;
    await h.poll();
    assert.equal(h.view.state.status, 'waiting');
    h.model.userStatus = 'approved';
    await h.poll();
    assert.equal(h.view.state.status, 'ready');
    assert.match(h.root.innerHTML, /No rooms yet/);
    assert.equal(h.timers.size, 0);
  });

  for (const kind of ['network', 'abort', 'auth']) test(`collab ${surface} approval poll handles ${kind} without losing lifecycle state`, async (t) => {
    const h = await collabHarness(t, surface, 'pending');
    await h.view.ready;
    const html = h.root.innerHTML;
    h.model.intercept = (path) => path !== '/me' ? null : kind === 'auth' ? jsonResponse({}, 401)
      : Promise.reject(kind === 'abort' ? new DOMException('cancelled', 'AbortError') : new TypeError('offline'));
    await h.poll();
    assert.equal(h.view.state.status, kind === 'auth' ? 'signed-out' : kind === 'abort' ? 'waiting' : 'degraded');
    if (kind === 'auth') assert.equal(h.timers.size, 0);
    else {
      if (kind === 'abort') assert.equal(h.root.innerHTML, html);
      h.model.intercept = () => null;
      h.model.userStatus = 'approved';
      await h.poll();
      assert.equal(h.view.state.status, 'ready');
      assert.equal(h.timers.size, 0);
    }
  });

  for (const outcome of ['success', 'error']) test(`collab ${surface} ignores approval polling ${outcome} after disposal`, async (t) => {
    const h = await collabHarness(t, surface, 'pending');
    await h.view.ready;
    const pending = deferred();
    h.model.intercept = (path) => path === '/me' ? pending.promise : null;
    const polling = h.poll();
    await flush();
    h.view.dispose();
    h.root.innerHTML = 'replacement view';
    const state = h.view.state;
    pending.resolve(outcome === 'success' ? jsonResponse({ user_id: 'u1', user_status: 'approved' })
      : jsonResponse({ error: 'stale polling failure' }, 503));
    await polling;
    assert.equal(h.root.innerHTML, 'replacement view');
    assert.deepEqual(h.view.state, state);
    assert.equal(h.timers.size, 0);
  });
}

const actionCases = [
  { name: 'project creation', route: '/rooms', path: '/projects', result: { project_id: 'p1' }, form: 'hcn-new' },
  { name: 'room creation', route: '/rooms', path: '/projects/p1/rooms', result: { room_id: 'created' }, form: 'hcn-new' },
  { name: 'People decision', route: '/people', path: '/admin/users/u2/approve', result: {}, button: 'decision' },
  { name: 'agent credential', route: '/agents', path: '/agents/connect', result: { credential: 'private-credential', device_id: 'd1', agent_id: 'a1' }, button: 'hcn-connect' },
  { name: 'fleet token', route: '/agents', path: '/agents/join-tokens', result: { token: 'private-token', max_uses: 1, name_prefix: 'worker', harness_type: 'codex' }, form: 'hcn-fleet-form', how: 'token' },
  { name: 'fleet credentials', route: '/agents', path: '/agents/bulk', result: { agents: [] }, form: 'hcn-fleet-form', how: 'direct' },
  { name: 'fleet spawn', route: '/agents', path: '/fleet/spawns', result: {}, form: 'hcn-fleet-form', how: 'spawn' },
];

async function actionHarness(t, surface, action) {
  const responses = (path, init = {}) => {
    if (path === '/me') return jsonResponse({ user_id: 'u1', user_status: 'approved', approver: true });
    if (path.startsWith('/admin/users') && init.method === 'GET') return jsonResponse({ users: [{ user_id: 'u2', email: 'two@example.org', status: 'pending' }], can_manage: false });
    if (init.method === 'POST') return jsonResponse(path === '/projects' ? { project_id: 'p1' }
      : path === '/projects/p1/rooms' ? { room_id: 'created' } : action.result);
    if (path === '/agents/join-tokens') return jsonResponse({ tokens: [] });
    return null;
  };
  const h = await collabHarness(t, surface, 'approved', responses);
  await h.view.ready;
  const realFormData = globalThis.FormData;
  t.mock.method(globalThis, 'FormData', class { constructor(form) { return form.formData; } });
  for (const id of ['hcn-new-msg', 'hcn-user-note', 'hcn-people-msg', 'hcn-agent-name', 'hcn-connect-out',
    'hcn-fleet', 'hcn-fleet-out', 'hcn-fleet-form', 'hcn-fleet-msg', 'hcn-tokens', 'hcn-copy', 'hcn-snippet', 'hcn-note']) {
    h.elements.set(id, h.element());
  }
  for (const id of ['hcn-new', 'hcn-fleet-form']) {
    const formData = new realFormData();
    for (const [key, value] of Object.entries({ name: 'New room', family: 'codex', prefix: 'worker', count: '1', how: action.how || 'token' })) formData.set(key, value);
    h.elements.get(id).formData = formData;
  }
  const decision = { ...h.element(), dataset: { act: 'approve', user: 'u2' },
    closest: () => ({ querySelector: () => ({ textContent: 'two@example.org' }) }) };
  h.elements.set('decision', decision);
  h.document.querySelectorAll = (selector) => selector === '[data-act]' ? [decision] : [];
  await h.navigate(action.route);
  assert.equal(h.view.state.status, 'ready', 'action route must finish mounting');
  return { ...h, responses, invoke: () => action.form
    ? h.elements.get(action.form).onsubmit({ preventDefault() {}, target: h.elements.get(action.form) })
    : h.elements.get(action.button).onclick() };
}

const actionSnapshot = (h) => ({ html: h.root.innerHTML, state: h.view.state,
  hash: h.window.location.hash, credentials: [...h.storage], timers: [...h.timers.keys()], calls: [...h.calls],
  elements: [...h.elements].map(([id, el]) => [id, el.innerHTML, el.textContent, el.disabled, el.value]),
});

for (const surface of ['public', 'dashboard']) for (const action of actionCases) {
  for (const leave of ['navigation', 'disposal']) for (const outcome of ['success', 'error', 'auth', 'abort']) {
    test(`collab ${surface} action ${action.name} ignores ${outcome} after ${leave}`, async (t) => {
      const h = await actionHarness(t, surface, action);
      const pending = deferred();
      let held = false;
      h.model.intercept = (path, init) => {
        if (path === action.path && init.method === 'POST') { held = true; return pending.promise; }
        return h.responses(path, init);
      };
      const completion = h.invoke();
      await flush();
      assert.ok(held, 'the intended mutation is in flight');
      if (leave === 'navigation') await h.navigate('/room/new');
      else { h.view.dispose(); h.root.innerHTML = 'replacement view'; }
      const snapshot = actionSnapshot(h);
      if (outcome === 'abort') pending.reject(new DOMException('cancelled', 'AbortError'));
      else pending.resolve(jsonResponse(outcome === 'success' ? action.result : { error: 'late action failure' },
        outcome === 'auth' ? 401 : outcome === 'error' ? 503 : 200));
      await completion;
      await flush();
      assert.deepEqual(actionSnapshot(h), snapshot, 'stale action cannot navigate, render, refresh, mutate credentials, or schedule work');
    });
  }

  test(`collab ${surface} action ${action.name} still completes on its current route`, async (t) => {
    const h = await actionHarness(t, surface, action);
    const before = h.calls.length;
    await h.invoke();
    await flush();
    assert.ok(h.calls.slice(before).includes(action.path));
    if (action.route === '/rooms') {
      if (surface === 'public') assert.equal(h.window.location.hash, '/room/created');
      else assert.equal(h.view.state.roomId, 'created');
    } else if (action.name === 'People decision') assert.match(h.root.innerHTML, /<h1>People/);
    else if (action.name === 'agent credential') assert.match(h.elements.get('hcn-snippet').textContent, /private-credential/);
    else if (action.name === 'fleet spawn') assert.match(h.elements.get('hcn-fleet-msg').textContent, /Requested/);
    else assert.match(h.elements.get('hcn-fleet-out').innerHTML, /hd-card/);
  });

  test(`collab ${surface} action ${action.name} still reports current errors`, async (t) => {
    const h = await actionHarness(t, surface, action);
    h.model.intercept = (path, init) => path === action.path && init?.method === 'POST'
      ? jsonResponse({ error: 'current action failure' }, 503) : h.responses(path, init);
    await h.invoke();
    const id = action.route === '/rooms' ? 'hcn-new-msg' : action.name === 'People decision' ? 'hcn-people-msg'
      : action.name === 'agent credential' ? 'hcn-connect-out' : 'hcn-fleet-msg';
    const el = h.elements.get(id);
    assert.match(el.innerHTML + el.textContent, /current action failure/);
  });
}

for (const surface of ['public', 'dashboard']) for (const leave of ['navigation', 'disposal']) {
  test(`collab ${surface} credential copy completion cannot change labels or timers after ${leave}`, async (t) => {
    const h = await actionHarness(t, surface, actionCases.find((action) => action.name === 'agent credential'));
    await h.invoke();
    const pending = deferred();
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
    Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { clipboard: { writeText: () => pending.promise } } });
    t.after(() => { if (descriptor) Object.defineProperty(globalThis, 'navigator', descriptor); else delete globalThis.navigator; });
    const completion = h.elements.get('hcn-copy').onclick();
    if (leave === 'navigation') await h.navigate('/room/new');
    else h.view.dispose();
    const snapshot = actionSnapshot(h);
    pending.resolve();
    await completion;
    assert.deepEqual(actionSnapshot(h), snapshot);
  });
}

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
