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
