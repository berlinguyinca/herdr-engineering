import test from 'node:test';
import assert from 'node:assert/strict';
import {
  renderCollaboratorRow, renderGrantForm, renderInviteForm, renderInviteResult, renderRestrictAll, mountCollaborators,
} from './artifact-collaborators.js';

const IMG = '<img src=x onerror=alert(1)>';
const CODE = 'INVITE-CODE-s3cr3t';
const assertInert = html => {
  assert.doesNotMatch(html, /<img/i);
  assert.doesNotMatch(html, /<script/i);
  assert.doesNotMatch(html, /<(iframe|frame|object|embed)/i);
  // real handler attributes only: strip quoted values and text nodes, where escaped payloads legitimately sit
  assert.doesNotMatch(html.replace(/"[^"]*"/g, '""').replace(/>[^<]*</g, '><'), /\son[a-z]+=/i);
};
const tick = () => new Promise(resolve => setTimeout(resolve, 0));

const collab = (extra = {}) => ({
  agent_id: 'agent-1', permission: 'write', expires_at: '2026-12-01T00:00:00Z', last_used_at: '2026-11-01T10:00:00Z',
  commits: 3, conflicts: 1, ...extra,
});

// ------------------------------------------------------------------ renderers

test('a collaborator row shows agent, permission, expiry, last used, commits, conflicts and Revoke', () => {
  const html = renderCollaboratorRow(collab(), { busy: false });
  assert.match(html, /agent-1/);
  assert.match(html, /write/);
  assert.match(html, /2026-12-01/);
  assert.match(html, /2026-11-01/);
  assert.match(html, /data-collab-action="revoke"[^>]*data-agent-id="agent-1"/);
  assert.doesNotMatch(html, /disabled/);
  assert.match(renderCollaboratorRow(collab(), { busy: true }), /<button[^>]*disabled/);
});

test('a hostile agent name renders inert in the row, the pickers and the id attribute', () => {
  assertInert(renderCollaboratorRow(collab({ agent_id: IMG, display_name: IMG, permission: IMG, expires_at: IMG, last_used_at: IMG }), {}));
  const html = renderGrantForm({ agents: [{ agent_id: IMG, name: IMG }], form: { agent_id: IMG }, writable: true, form_days: 30 });
  assertInert(html);
  assertInert(renderInviteForm({ rooms: [{ room_id: IMG, name: IMG }], form: { room: IMG, label: IMG } }));
});

test('the grant form has an agent select, Read / Read+write, and days 1-90 defaulting to 30', () => {
  const html = renderGrantForm({ agents: [{ agent_id: 'a1', name: 'Alpha' }], form: {}, writable: true });
  assert.match(html, /<select[^>]*data-collab-field="agent_id"/);
  assert.match(html, /<option value="a1"[^>]*>Alpha/);
  const days = html.match(/<input[^>]*data-collab-field="days"[^>]*>/)?.[0];
  assert.match(days, /type="number"/);
  assert.match(days, /min="1"/);
  assert.match(days, /max="90"/);
  assert.match(days, /value="30"/);
  assert.doesNotMatch(html, /value="write"[^>]*disabled/);
});

test('a non-writable artifact disables Read+write and shows writable_reason', () => {
  const html = renderGrantForm({ agents: [], form: {}, writable: false, writableReason: 'artifact is not a text bundle' });
  assert.match(html, /value="write"[^>]*disabled/);
  assert.match(html, /artifact is not a text bundle/);
  assert.doesNotMatch(html, /value="read"[^>]*disabled/);
});

test('failed agent lookup falls back to an agent-id text input', () => {
  const html = renderGrantForm({ agents: null, form: {}, writable: true });
  assert.match(html, /<input[^>]*type="text"[^>]*data-collab-field="agent_id"/);
  assert.doesNotMatch(html, /<select[^>]*data-collab-field="agent_id"/);
});

test('the invite form has a room select; the invite result never carries the code in HTML', () => {
  const form = renderInviteForm({ rooms: [{ room_id: 'r1', name: 'Room One' }], form: {} });
  assert.match(form, /<select[^>]*data-collab-field="room"/);
  assert.match(form, /<option value="r1"[^>]*>Room One/);
  const result = renderInviteResult({ code: CODE, expires_at: 1900000000 });
  assert.doesNotMatch(result, new RegExp(CODE));
  assert.match(result, /data-invite-code/);
  assert.match(result, /data-collab-action="close-invite"/);
  assert.match(result, /shown only once|cannot be shown again/i);
});

test('Restrict all access needs a confirm step and highlights after a retraction offer', () => {
  const idle = renderRestrictAll({});
  assert.match(idle, /Restrict all access/);
  assert.match(idle, /data-collab-action="restrict"/);
  assert.doesNotMatch(idle, /restrict-confirm/);
  assert.doesNotMatch(idle, /data-restrict-offered/);
  const confirming = renderRestrictAll({ confirming: true });
  assert.match(confirming, /data-collab-action="restrict-confirm"/);
  assert.match(confirming, /data-collab-action="restrict-cancel"/);
  assert.match(renderRestrictAll({ offered: true }), /data-restrict-offered/);
  assert.match(renderRestrictAll({ confirming: true, busy: true }), /restrict-confirm"[^>]*disabled/);
});

// ------------------------------------------------------------------ mount harness

function fakeContainer() {
  const listeners = {};
  let html = '';
  let inputs = {};
  const written = [];
  const dataset = (selector, data) => {
    const attr = selector.match(/^\[data-([a-z-]+)\]$/)?.[1];
    if (!attr) return null;
    const key = attr.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    return key in data ? { dataset: data, disabled: false, value: data.value } : null;
  };
  const container = {
    isConnected: true,
    written,
    get innerHTML() { return html; },
    set innerHTML(value) { html = String(value); inputs = {}; written.push(html); },
    replaceChildren() { container.innerHTML = ''; },
    querySelector(selector) {
      if (selector === '[data-invite-code]') return html.includes('data-invite-code') ? (inputs.code ||= { value: '', focus() {}, select() {} }) : null;
      return null;
    },
    contains: () => true,
    addEventListener(type, fn) { (listeners[type] ||= new Set()).add(fn); },
    removeEventListener(type, fn) { listeners[type]?.delete(fn); },
    listenerCount: () => Object.values(listeners).reduce((n, set) => n + set.size, 0),
    click(data) { for (const fn of listeners.click || []) fn({ target: { closest: s => dataset(s, data) } }); },
    change(field, value) { for (const type of ['input', 'change']) for (const fn of listeners[type] || []) fn({ target: { closest: s => dataset(s, { collabField: field, value }) } }); },
    submit() { for (const fn of listeners.submit || []) fn({ target: { closest: () => ({}) }, preventDefault() {} }); },
  };
  return container;
}

function fakeClient({ list, listError, grant, revoke, inviteGrant, restrict } = {}) {
  const calls = [];
  const log = (name, ...args) => calls.push([name, ...args]);
  const gate = {};
  const client = {
    calls,
    count: name => calls.filter(c => c[0] === name).length,
    hold: name => { gate[name] = Promise.withResolvers(); return gate[name]; },
    async listArtifactCollaborators(...a) {
      log('list', ...a.slice(0, 1));
      if (listError) throw listError;
      return list ?? { writable: true, writable_reason: null, collaborators: [], pending_invites: [] };
    },
    async grantArtifactCollaborator(id, body) { log('grant', id, body); return grant ?? { agent_id: body.agent_id }; },
    async revokeArtifactCollaborator(id, agent) { log('revoke', id, agent); if (gate.revoke) await gate.revoke.promise; return revoke ?? { revoked: true }; },
    async grantArtifactInvite(id, body) { log('inviteGrant', id, body); return inviteGrant ?? {}; },
    async restrictArtifactCollab(id) { log('restrict', id); if (gate.restrict) await gate.restrict.promise; return restrict ?? { revoked: 2, pending_invites_revoked: 1 }; },
  };
  return client;
}

function fakeCollab({ agentsError, roomsError, invite } = {}) {
  const calls = [];
  return {
    calls,
    async agents() { calls.push('agents'); if (agentsError) throw agentsError; return { agents: [{ agent_id: 'ag1', name: 'Alpha', external: false }] }; },
    async rooms() { calls.push('rooms'); if (roomsError) throw roomsError; return { rooms: [{ room_id: 'r1', name: 'Room One' }] }; },
    async inviteExternal(room, body) { calls.push(['invite', room, body]); return invite ?? { invite_id: 'inv1', code: CODE, expires_at: 1900000000, room_id: room }; },
  };
}

const status503 = Object.assign(new Error('collaboration is not enabled'), { status: 503 });

// ------------------------------------------------------------------ mount behaviour

test('the mount lists collaborators and pending invites, escaped', async () => {
  const container = fakeContainer();
  const client = fakeClient({ list: { writable: true, collaborators: [collab({ agent_id: IMG })], pending_invites: [{ invite_id: IMG, permission: 'read', expires_at: '2027-01-01' }] } });
  const panel = mountCollaborators(container, client, fakeCollab(), 'art1', {});
  await tick(); await tick();
  assert.match(container.innerHTML, /data-collab-action="revoke"/);
  assert.match(container.innerHTML, /&lt;img/);
  assertInert(container.innerHTML);
  for (const html of container.written) assertInert(html);
  panel.dispose();
});

test('a 503 shows Collaboration is not enabled with the form hidden', async () => {
  const container = fakeContainer();
  const panel = mountCollaborators(container, fakeClient({ listError: status503 }), fakeCollab(), 'art1', {});
  await tick(); await tick();
  assert.match(container.innerHTML, /Collaboration is not enabled/);
  assert.doesNotMatch(container.innerHTML, /data-collab-field/);
  assert.doesNotMatch(container.innerHTML, /Restrict all access/);
  panel.dispose();
});

test('a failed CollabApi.agents() shows the agent-id text input', async () => {
  const container = fakeContainer();
  const panel = mountCollaborators(container, fakeClient(), fakeCollab({ agentsError: new Error('down') }), 'art1', {});
  await tick(); await tick();
  assert.match(container.innerHTML, /<input[^>]*type="text"[^>]*data-collab-field="agent_id"/);
  panel.dispose();
});

test('granting posts the form values and refreshes the list', async () => {
  const container = fakeContainer();
  const client = fakeClient();
  const panel = mountCollaborators(container, client, fakeCollab(), 'art1', {});
  await tick(); await tick();
  container.change('agent_id', 'ag1'); container.change('permission', 'write'); container.change('days', '7');
  container.click({ collabAction: 'grant' });
  await tick(); await tick();
  assert.deepEqual(client.calls.find(c => c[0] === 'grant'), ['grant', 'art1', { agent_id: 'ag1', permission: 'write', expires_in_days: 7 }]);
  assert.equal(client.count('list'), 2);
  panel.dispose();
});

test('an out-of-range day count is refused before any call', async () => {
  const container = fakeContainer();
  const client = fakeClient();
  const panel = mountCollaborators(container, client, fakeCollab(), 'art1', {});
  await tick(); await tick();
  container.change('agent_id', 'ag1'); container.change('days', '91');
  container.click({ collabAction: 'grant' });
  await tick();
  assert.equal(client.count('grant'), 0);
  assert.match(container.innerHTML, /1.{1,3}90/);
  panel.dispose();
});

test('a write grant on a non-writable artifact is never sent', async () => {
  const container = fakeContainer();
  const client = fakeClient({ list: { writable: false, writable_reason: 'binary bundle', collaborators: [], pending_invites: [] } });
  const panel = mountCollaborators(container, client, fakeCollab(), 'art1', {});
  await tick(); await tick();
  assert.match(container.innerHTML, /binary bundle/);
  container.change('agent_id', 'ag1'); container.change('permission', 'write');
  container.click({ collabAction: 'grant' });
  await tick();
  assert.equal(client.count('grant'), 0);
  panel.dispose();
});

test('buttons are disabled while a revoke is in flight and a second click sends nothing', async () => {
  const container = fakeContainer();
  const client = fakeClient({ list: { writable: true, collaborators: [collab()], pending_invites: [] } });
  const panel = mountCollaborators(container, client, fakeCollab(), 'art1', {});
  await tick(); await tick();
  const gate = client.hold('revoke');
  container.click({ collabAction: 'revoke', agentId: 'agent-1' });
  container.click({ collabAction: 'revoke', agentId: 'agent-1' });
  await tick();
  assert.equal(client.count('revoke'), 1);
  assert.match(container.innerHTML, /<button[^>]*data-collab-action="revoke"[^>]*disabled/);
  assert.match(container.innerHTML, /<button[^>]*data-collab-action="grant"[^>]*disabled/);
  gate.resolve();
  await tick(); await tick();
  assert.doesNotMatch(container.innerHTML, /<button[^>]*data-collab-action="revoke"[^>]*disabled/);
  panel.dispose();
});

test('Restrict all access is never called without a click and a confirm, and then exactly once', async () => {
  const container = fakeContainer();
  const client = fakeClient();
  const panel = mountCollaborators(container, client, fakeCollab(), 'art1', {});
  await tick(); await tick();
  panel.offerRestrictAll();                       // a retraction offer only highlights
  await tick();
  assert.match(container.innerHTML, /data-restrict-offered/);
  assert.equal(client.count('restrict'), 0);
  container.click({ collabAction: 'restrict' });
  await tick();
  assert.equal(client.count('restrict'), 0);
  assert.match(container.innerHTML, /data-collab-action="restrict-confirm"/);
  container.click({ collabAction: 'restrict-cancel' });
  await tick();
  assert.equal(client.count('restrict'), 0);
  assert.doesNotMatch(container.innerHTML, /restrict-confirm/);
  container.click({ collabAction: 'restrict' });
  const gate = client.hold('restrict');
  container.click({ collabAction: 'restrict-confirm' });
  container.click({ collabAction: 'restrict-confirm' });
  await tick();
  assert.equal(client.count('restrict'), 1);
  gate.resolve();
  await tick(); await tick();
  assert.equal(client.count('restrict'), 1);
  assert.match(container.innerHTML, /2 grants? revoked|Revoked 2/i);
  container.click({ collabAction: 'restrict-confirm' });     // a stray confirm after the call authorises nothing
  await tick(); await tick();
  assert.equal(client.count('restrict'), 1);
  panel.dispose();
});

test('a confirm click without the ask step calls nothing', async () => {
  const container = fakeContainer();
  const client = fakeClient();
  const panel = mountCollaborators(container, client, fakeCollab(), 'art1', {});
  await tick(); await tick();
  container.click({ collabAction: 'restrict-confirm' });
  await tick(); await tick();
  assert.equal(client.count('restrict'), 0);
  panel.dispose();
});

test('the invite code lives only in the input value and is gone after Close', async () => {
  const container = fakeContainer();
  const client = fakeClient();
  const collabApi = fakeCollab();
  const panel = mountCollaborators(container, client, collabApi, 'art1', {});
  await tick(); await tick();
  container.change('room', 'r1'); container.change('invite_permission', 'read'); container.change('invite_days', '14');
  container.click({ collabAction: 'invite' });
  await tick(); await tick(); await tick();
  assert.deepEqual(collabApi.calls.find(c => c[0] === 'invite')[1], 'r1');
  assert.deepEqual(client.calls.find(c => c[0] === 'inviteGrant'), ['inviteGrant', 'art1', { invite_id: 'inv1', permission: 'read', expires_in_days: 14 }]);
  assert.equal(container.querySelector('[data-invite-code]').value, CODE);
  container.click({ collabAction: 'revoke', agentId: 'nobody' });   // a re-render keeps the code in the box
  await tick(); await tick();
  assert.equal(container.querySelector('[data-invite-code]').value, CODE);
  container.click({ collabAction: 'close-invite' });
  await tick();
  assert.equal(container.querySelector('[data-invite-code]'), null);
  for (const html of container.written) assert.doesNotMatch(html, new RegExp(CODE));
  panel.dispose();
});

test('dispose removes listeners and the content', async () => {
  const container = fakeContainer();
  const panel = mountCollaborators(container, fakeClient(), fakeCollab(), 'art1', {});
  await tick(); await tick();
  panel.dispose();
  assert.equal(container.innerHTML, '');
  assert.equal(container.listenerCount(), 0);
});

test('sparse objects render an em dash, never the text "undefined"', () => {
  const html = renderCollaboratorRow({}, {}) + renderInviteForm({ rooms: [{}], form: {} });
  assert.doesNotMatch(html, /undefined|null/);
  assert.match(html, /—/);
  assert.match(renderInviteForm({ rooms: [], form: {} }), /data-collab-field="label"[^>]*maxlength="40"/);
});
