import test from 'node:test';
import assert from 'node:assert/strict';
import { edgeState, layoutFlow } from '../collab-flow.js';

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
