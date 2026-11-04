import test from 'node:test';
import assert from 'node:assert/strict';
import { buildTeamMap, describeTeamMap, edgeMeaning, layoutNodes, effectiveLayout, progressOf } from '../team-map-model.js';

const node = (id, kind, over = {}) => ({ id, kind, display_name: id.split(':')[1], status: 'open', tombstone: false, ...over });
const edge = (id, type, from, to, live = true) => ({ id, type, from_id: from, to_id: to, live });
const SNAP = {
  nodes: [node('project:p', 'project'), node('issue:i1', 'issue'), node('task:t1', 'task', { status: 'done' }), node('task:t2', 'task', { status: 'blocked' }),
    node('artifact:a1', 'artifact'), node('agent_session:s1', 'agent_session'), node('review:r1', 'review')],
  edges: [edge('e1', 'contains', 'project:p', 'issue:i1'), edge('e2', 'contains', 'issue:i1', 'task:t1'), edge('e3', 'contains', 'issue:i1', 'task:t2'),
    edge('e4', 'produced', 'task:t1', 'artifact:a1'), edge('e5', 'reviews', 'review:r1', 'task:t1'), edge('e6', 'depends_on', 'task:t2', 'task:t1', false)],
};

test('lines mean one of five things, and anything unknown is coordination', () => {
  assert.deepEqual(['delegated_to', 'depends_on', 'produced', 'reviews', 'discussed_in', 'nonsense'].map(edgeMeaning),
    ['delegation', 'dependency', 'produced', 'reviewed', 'coordination', 'coordination']);
});

test('the map groups project -> issue -> task -> deliverables and counts milestones, never a percentage', () => {
  const m = buildTeamMap(SNAP);
  assert.equal(m.groups.length, 1);
  const issue = m.groups[0].children[0];
  assert.equal(issue.node.id, 'issue:i1');
  assert.deepEqual(issue.children.map((c) => c.node.id), ['task:t1', 'task:t2']);
  assert.equal(issue.children[0].children[0].node.id, 'artifact:a1', 'the artifact hangs off the task that produced it');
  assert.equal(issue.progress.milestones, '1 of 2 tasks done');
  assert.equal(issue.progress.label, '1 blocked');
  assert.doesNotMatch(JSON.stringify(m.groups[0].progress), /%/);
  assert.equal(progressOf([]).label, 'No tasks yet');
});

test('noisy execution nodes are counted and folded away until asked for', () => {
  assert.equal(buildTeamMap(SNAP).hidden.execution, 1);
  const flat = (t) => [t.node.id, ...t.children.flatMap(flat)];
  assert.ok(!buildTeamMap(SNAP).groups.flatMap(flat).includes('agent_session:s1'));
  assert.ok(buildTeamMap(SNAP, { showExecution: true }).unplaced.flatMap(flat).includes('agent_session:s1'));
});

test('filters narrow by kind, status and text, and tombstoned things never show', () => {
  const ids = (m) => [...m.groups, ...m.unplaced].flatMap(function f(t) { return [t.node.id, ...t.children.flatMap(f)]; });
  assert.deepEqual(ids(buildTeamMap(SNAP, { kinds: new Set(['task']) })).sort(), ['task:t1', 'task:t2']);
  assert.deepEqual(ids(buildTeamMap(SNAP, { status: 'done', kinds: new Set(['task']) })), ['task:t1']);
  assert.ok(ids(buildTeamMap(SNAP, { query: 'A1' })).includes('artifact:a1'));
  const gone = { ...SNAP, nodes: [...SNAP.nodes, node('task:dead', 'task', { tombstone: true })] };
  assert.ok(!ids(buildTeamMap(gone)).includes('task:dead'));
});

test('the text description names every relationship and marks the ones that no longer hold', () => {
  const d = describeTeamMap(buildTeamMap(SNAP));
  assert.ok(d.lines.some((l) => /^issue i1 \(open\) — 1 of 2 tasks done/.test(l.text)));
  assert.ok(d.lines.some((l) => l.depth === 3 && /artifact a1/.test(l.text)));
  assert.ok(d.relations.some((r) => r.text === 't1 produced a1' && r.live));
  assert.ok(d.relations.some((r) => /t2 waits for t1 \(earlier, no longer current\)/.test(r.text) && !r.live));
});

test('layout is deterministic, keeps placed nodes still and puts new ones in the next free row of their column', () => {
  const nodes = SNAP.nodes;
  const first = layoutNodes(nodes);
  assert.deepEqual(layoutNodes(nodes), first, 'same input, same output');
  assert.deepEqual(layoutNodes(nodes.slice().reverse()), first, 'input order does not matter');
  const grown = layoutNodes([...nodes, node('task:t3', 'task')], first);
  for (const n of nodes) assert.deepEqual(grown[n.id], first[n.id], `${n.id} did not move`);
  assert.equal(grown['task:t3'].x, first['task:t1'].x);
  assert.ok(![first['task:t1'].y, first['task:t2'].y].includes(grown['task:t3'].y));
});

test('a personal position beats a shared one, which beats the automatic one, node by node', () => {
  const auto = { a: { x: 0, y: 0 }, b: { x: 1, y: 1 }, c: { x: 2, y: 2 } };
  const out = effectiveLayout(auto, { b: { x: 9, y: 9 }, c: { x: 8, y: 8 } }, { c: { x: 7, y: 7 } });
  assert.deepEqual(out, { a: { x: 0, y: 0 }, b: { x: 9, y: 9 }, c: { x: 7, y: 7 } });
});

test('a large graph is grouped, described and laid out quickly', () => {
  const nodes = [node('project:p', 'project')]; const edges = [];
  for (let i = 0; i < 400; i += 1) {
    nodes.push(node(`issue:i${i}`, 'issue')); edges.push(edge(`ei${i}`, 'contains', 'project:p', `issue:i${i}`));
    for (let j = 0; j < 9; j += 1) { nodes.push(node(`task:t${i}-${j}`, 'task')); edges.push(edge(`et${i}-${j}`, 'contains', `issue:i${i}`, `task:t${i}-${j}`)); }
  }
  const started = performance.now();
  const map = buildTeamMap({ nodes, edges });
  const text = describeTeamMap(map);
  const placed = layoutNodes(nodes);
  const ms = performance.now() - started;
  assert.equal(text.lines.length, 4001);
  assert.equal(Object.keys(placed).length, 4001);
  assert.ok(ms < 1500, `4,001 nodes took ${Math.round(ms)} ms`);
});
