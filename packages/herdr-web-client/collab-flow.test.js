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

test('a delegation chain reads left to right and idle agents wait in column one', () => {
  const lay = layoutFlow(flow);
  const x = Object.fromEntries(lay.nodes.map((n) => [n.id, n.x]));
  assert.ok(x['u:alice'] < x['a:1'] && x['a:1'] < x['a:2']);
  assert.equal(x['a:3'], x['a:1']);
  assert.equal(lay.edges.length, 2);
  assert.ok(lay.edges.every((e) => e.d.startsWith('M')));
});

test('edge state follows open work, then failures', () => {
  assert.deepEqual(edgeState({ pending: 1, done: 3 }), { state: 'pending', live: true, total: 4 });
  assert.equal(edgeState({ accepted: 1 }).state, 'accepted');
  assert.equal(edgeState({ failed: 1, done: 2 }).state, 'failed');
  assert.equal(edgeState({ done: 2 }).live, false);
});

test('dozens of agents stay in bounded columns and edges to unknown nodes are dropped', () => {
  const many = { nodes: [{ id: 'u:a', kind: 'person', name: 'a' }], edges: [{ from: 'u:a', to: 'a:ghost', counts: { done: 1 }, last: '' }] };
  for (let i = 0; i < 60; i += 1) many.nodes.push({ id: `a:${i}`, kind: 'agent', name: `w${i}`, family: 'codex' });
  const lay = layoutFlow(many);
  assert.equal(lay.nodes.length, 61);
  assert.equal(lay.edges.length, 0);
  assert.ok(lay.height >= 60 * 46 && lay.width < 1200);
});
