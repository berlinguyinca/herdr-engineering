import test from 'node:test';
import assert from 'node:assert/strict';
import { buildTabsPanes, nextTab, renderTabsPanes } from '../collab-tabs.js';

const agent = (id, extra = {}, state = 'running') => ({ id: `agent:${id}`, node_type: 'agent', label: id, state, attrs: extra });
const spawn = (a, b) => ({ edge_type: 'spawn', from_id: `agent:${a}`, to_id: `agent:${b}` });
const links = (grants, ws = 'ws-1', can_terminal = false) => [{ workspace_id: ws, grants, can_terminal }];

test('primary agents are tabs, spawned subagents are panes under the nearest tab, grandchildren nest', () => {
  const m = buildTabsPanes([{ nodes: [agent('lead'), agent('coder'), agent('tester'), agent('solo')],
    edges: [spawn('lead', 'coder'), spawn('coder', 'tester')] }]);
  assert.deepEqual(m.tabs.map((t) => t.label), ['lead', 'solo']);
  const lead = m.tabs[0];
  assert.deepEqual(lead.panes.map((p) => [p.label, p.depth]), [['coder', 1], ['tester', 2]]);
  assert.equal(m.tabs[1].panes.length, 0);
});

test('nothing is inferred: no spawn edge means no pane, and a removed or non-spawn edge never nests', () => {
  const m = buildTabsPanes([{ nodes: [agent('a'), agent('b'), agent('c')],
    edges: [{ edge_type: 'consultation', from_id: 'agent:a', to_id: 'agent:b' },
      { ...spawn('a', 'c'), removed: true }] }]);
  assert.equal(m.tabs.length, 3);
  assert.ok(m.tabs.every((t) => t.panes.length === 0));
});

test('a long-lived or external agent keeps its spawn lineage but gets its own tab', () => {
  const m = buildTabsPanes([{ nodes: [agent('lead'), agent('daemon', { presentation: 'tab' }),
    agent('remote', { external: true, reported_by: 'coworker-claude' }), agent('kid')],
  edges: [spawn('lead', 'daemon'), spawn('lead', 'remote'), spawn('daemon', 'kid')] }]);
  const by = Object.fromEntries(m.tabs.map((t) => [t.label, t]));
  assert.deepEqual(Object.keys(by).sort(), ['daemon', 'lead', 'remote']);
  assert.equal(by.daemon.parent, 'agent:lead');
  assert.deepEqual(by.daemon.panes.map((p) => p.label), ['kid']);
  assert.equal(by.remote.external, true);
  assert.equal(by.remote.can_terminal, false);
});

test('closing a pane never removes or changes the agent', () => {
  const nodes = [agent('lead'), agent('kid', {}, 'running'),
    { id: 'pane:p1', node_type: 'pane', state: 'closed', attrs: { agent_id: 'agent:kid' } }];
  const m = buildTabsPanes([{ nodes, edges: [spawn('lead', 'kid')] }]);
  const kid = m.tabs[0].panes[0];
  assert.equal(kid.state, 'running');
  assert.equal(kid.surface.closed, true);
  assert.match(renderTabsPanes(m), /Pane closed\. The agent keeps running/);
  const open = buildTabsPanes([{ nodes: nodes.map((n) => (n.id === 'pane:p1' ? { ...n, state: 'open' } : n)), edges: [spawn('lead', 'kid')] }]);
  assert.equal(open.tabs[0].panes[0].state, kid.state);
  assert.equal(open.tabs[0].panes[0].surface.closed, false);
});

test('a spawn cycle cannot hide agents: the cyclic edge is ignored', () => {
  const m = buildTabsPanes([{ nodes: [agent('a'), agent('b')], edges: [spawn('a', 'b'), spawn('b', 'a')] }]);
  const all = m.tabs.flatMap((t) => [t.label, ...t.panes.map((p) => p.label)]).sort();
  assert.deepEqual(all, ['a', 'b']);
});

test('workspace link grants decide what is shown and the hidden count leaks no names', () => {
  const g = [{ nodes: [agent('lead', { workspace_id: 'ws-1' }), agent('kid', { workspace_id: 'ws-1' }), agent('other', { workspace_id: 'ws-2' })],
    edges: [spawn('lead', 'kid')] }];
  const tabsOnly = buildTabsPanes(g, links(['tabs']));
  assert.deepEqual(tabsOnly.tabs.map((t) => t.label), ['lead']);
  assert.equal(tabsOnly.tabs[0].panes.length, 0);
  assert.equal(tabsOnly.hidden_by_grant, 2);
  const html = renderTabsPanes(tabsOnly);
  assert.ok(!html.includes('other') && !html.includes('kid'));
  assert.match(html, /2 more are not shared/);
  const both = buildTabsPanes(g, links(['tabs', 'panes']));
  assert.equal(both.tabs[0].panes.length, 1);
  assert.equal(buildTabsPanes(g, links([])).tabs.length, 0);
});

test('the page never offers a terminal and escapes everything it renders', () => {
  const m = buildTabsPanes([{ nodes: [agent('<img onerror=x>', { external: true, reported_by: '<b>x</b>' }), agent('k"id')],
    edges: [spawn('<img onerror=x>', 'k"id')] }]);
  const html = renderTabsPanes(m);
  assert.ok(!html.includes('<img') && !html.includes('<b>x'));
  assert.ok(html.includes('&lt;img onerror=x&gt;'));
  assert.ok(!/terminal<|Open terminal|<a /i.test(html));
  assert.match(html, /There is no terminal for this agent/);
});

test('empty and fully hidden states say so; the tablist is keyboard operable', () => {
  assert.match(renderTabsPanes(buildTabsPanes([])), /No agents are shown/);
  const m = buildTabsPanes([{ nodes: [agent('a'), agent('b'), agent('c')], edges: [] }]);
  const html = renderTabsPanes(m, 'agent:b');
  assert.match(html, /role="tablist"/);
  assert.equal(html.match(/role="tab"/g).length, 3);
  assert.match(html, /data-tab="agent:b"[^>]*aria-selected="true"/);
  assert.equal(html.match(/tabindex="0"/g).length, 2, 'selected tab plus the panel');
  const ids = m.tabs.map((t) => t.agent_id);
  assert.equal(nextTab(ids, 'agent:c', 'ArrowRight'), 'agent:a');
  assert.equal(nextTab(ids, 'agent:a', 'ArrowLeft'), 'agent:c');
  assert.equal(nextTab(ids, 'agent:b', 'End'), 'agent:c');
  assert.equal(nextTab(ids, 'agent:b', 'x'), null);
});

test('merging several neighborhoods dedupes nodes and edges', () => {
  const one = { nodes: [agent('lead'), agent('kid')], edges: [spawn('lead', 'kid')] };
  const m = buildTabsPanes([one, one, { nodes: [agent('kid')], edges: [spawn('lead', 'kid')] }]);
  assert.equal(m.tabs.length, 1);
  assert.equal(m.tabs[0].panes.length, 1);
});

test('the graph list call is scoped to one room and uses the collab route', async () => {
  const { CollabApi } = await import('./collab.js');
  const calls = [];
  const api = new CollabApi(() => ({ access: 'T' }), async (url, init) => { calls.push([init.method, url]); return { status: 200, ok: true, json: async () => ({}) }; });
  await api.graphList('room 1', 'agent', 'spawn');
  await api.graphList('room 1', 'pane');
  assert.deepEqual(calls[0], ['GET', '/api/v1/collab/graph/nodes?node_type=agent&room_id=room+1&limit=500&edge_type=spawn']);
  assert.deepEqual(calls[1], ['GET', '/api/v1/collab/graph/nodes?node_type=pane&room_id=room+1&limit=500']);
});

test('a chain deeper than the limit and a cycle with no entry are listed flat, never dropped', () => {
  const names = Array.from({ length: 40 }, (_, i) => `n${String(i).padStart(2, '0')}`);
  const nodes = [agent('root'), ...names.map((n) => agent(n))];
  const edges = [spawn('root', names[0]), ...names.slice(1).map((n, i) => spawn(names[i], n))];
  const m = buildTabsPanes([{ nodes, edges }]);
  const placed = m.tabs.flatMap((t) => [t.label, ...t.panes.map((p) => p.label)]);
  const flat = m.unplaced.map((p) => p.label);
  assert.equal(placed.length + flat.length, 41, 'every agent is shown somewhere');
  assert.ok(flat.length > 0);
  assert.match(renderTabsPanes(m), /more agents are nested too deeply or cyclic/);
  const cyc = buildTabsPanes([{ nodes: [agent('x'), agent('y'), agent('z')], edges: [spawn('x', 'y'), spawn('y', 'z'), spawn('z', 'x')] }]);
  assert.equal(cyc.tabs.flatMap((t) => [t.label, ...t.panes.map((p) => p.label)]).length + cyc.unplaced.length, 3);
});

// ------------------------------------------------------------------ T10: external agents as virtual tabs
const row = (id, extra = {}) => ({
  agent_id: id, node_id: `agent:${id}`, kind: 'external', label: 'Dana', name: `ext-dana-${id}`, state: 'active',
  invited_by: 'alice', invited_by_email: 'alice@example.org', enrolled_at: '2026-10-23T10:00:00+00:00', expires_at: 1800086400,
  revoked_at: null, revoked_by: null, actions: ['read', 'post'], lineage: true, declared_harness: 'claude-code',
  presence: { state: 'online', status: 'working', last_activity: 'reading the spec', last_seen_at: '2026-10-23T10:05:00+00:00', age_seconds: 12 },
  terminal: false, ...extra });

test('an attested external agent gets a virtual Remote tab from the server roster even with no lineage at all', () => {
  const m = buildTabsPanes([{ nodes: [agent('lead')], edges: [] }], null, [row('agent_x1')]);
  const t = m.tabs.find((x) => x.agent_id === 'agent:agent_x1');
  assert.ok(t);
  assert.equal(t.external, true);
  assert.equal(t.attested, true);
  assert.equal(t.can_terminal, false);
  assert.equal(t.state, 'working');
  assert.equal(t.invited_by, 'alice@example.org');
  assert.deepEqual(t.reported, []);
  const html = renderTabsPanes(m, 'agent:agent_x1');
  assert.match(html, /Remote/);
  assert.match(html, /no terminal and no shell access/);
  assert.match(html, /Invited by alice@example\.org/);
  assert.match(html, /Online\. Last heartbeat 12 s ago/);
  assert.doesNotMatch(html, /data-revoke/);                 // only a room owner gets the button
});

test('stale presence is never shown as a live status, and a restart shows no heartbeat', () => {
  const stale = row('agent_s', { presence: { state: 'stale', status: null, last_status: 'working', last_activity: 'old', age_seconds: 400 } });
  const none = row('agent_n', { presence: { state: 'none', status: null } });
  const m = buildTabsPanes([], null, [stale, none]);
  const by = Object.fromEntries(m.tabs.map((t) => [t.raw_id, t]));
  assert.equal(by.agent_s.state, 'stale');
  assert.equal(by.agent_n.state, 'no heartbeat');
  const html = renderTabsPanes(m, 'agent:agent_s');
  assert.match(html, /stale\)\. Its last reported status is not shown as current/);
  assert.doesNotMatch(html, />working</);
  assert.match(renderTabsPanes(m, 'agent:agent_n'), /No heartbeat since this service started/);
});

test('a revoked external keeps its tab, marked revoked, and offers no revoke button', () => {
  const r = row('agent_r', { state: 'revoked', revoked_at: '2026-10-23T11:00:00+00:00', revoked_by: 'user:alice' });
  const m = buildTabsPanes([], null, [r]);
  assert.equal(m.tabs[0].state, 'revoked');
  const html = renderTabsPanes(m, null, { canManage: true });
  assert.match(html, /Access revoked 2026-10-23T11:00:00\+00:00 by user:alice/);
  assert.match(html, /what it already did stays visible/);
  assert.doesNotMatch(html, /data-revoke/);
  assert.match(renderTabsPanes(buildTabsPanes([], null, [row('agent_ok')]), null, { canManage: true }), /data-revoke="agent_ok"/);
});

test('provenance and Remote come from the server roster, never from attrs an emitter wrote', () => {
  // a node claiming not to be external, with the id of an attested external, is still that external
  const spoof = { id: 'agent:agent_e', node_type: 'agent', label: 'Totally the lead', state: 'done', attrs: { external: false, reported_by: 'nobody' } };
  const m = buildTabsPanes([{ nodes: [spoof], edges: [] }], null, [row('agent_e')]);
  const t = m.tabs[0];
  assert.equal(t.external, true);
  assert.equal(t.label, 'Dana');                     // the owner-written label, not the emitter's
  assert.equal(t.state, 'working');                  // presence, not the node's claimed state
  assert.equal(t.reported[0].label, 'Totally the lead');   // what it reported stays visible, marked as reported
  assert.match(renderTabsPanes(m), /reported by the agent/);
  // an unattested node that merely says it is external is labelled self-declared
  const self = buildTabsPanes([{ nodes: [agent('x', { external: true, reported_by: 'someone' })], edges: [] }]);
  assert.equal(self.tabs[0].attested, false);
  assert.match(renderTabsPanes(self), /self-declared/);
  // a row that is not an external, or lacks ids, is ignored
  assert.equal(buildTabsPanes([], null, [{ kind: 'agent', agent_id: 'a', node_id: 'agent:a' }, null, {}]).tabs.length, 0);
});

test('everything an external agent supplies is escaped, including labels, activity text and lineage', () => {
  const evil = '<img src=x onerror=alert(1)>"\'&';
  const r = row('agent_h', { label: evil, invited_by_email: evil, declared_harness: evil,
    presence: { state: 'online', status: 'working', last_activity: evil, last_seen_at: evil, age_seconds: 1 } });
  const node = { id: 'agent:agent_h.sub', node_type: 'attempt', label: evil, state: 'running', attrs: {} };
  const html = renderTabsPanes(buildTabsPanes([{ nodes: [node], edges: [] }], null, [r]), null, { canManage: true });
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img/);
  assert.match(html, /data-revoke="agent_h"/);
});

test('attested externals are not dropped by the agent limit and a pane under one nests normally', () => {
  const many = Array.from({ length: 520 }, (_, i) => agent(`a${String(i).padStart(3, '0')}`));
  const m = buildTabsPanes([{ nodes: many, edges: [] }], null, [row('zzz_last')]);
  assert.ok(m.tabs.some((t) => t.raw_id === 'zzz_last'));
  const child = agent('agent_p.kid');
  const m2 = buildTabsPanes([{ nodes: [child], edges: [spawn('agent_p', 'agent_p.kid')] }], null, [row('agent_p')]);
  assert.equal(m2.tabs[0].panes[0].label, 'agent_p.kid');
});
