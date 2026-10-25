import {test} from 'node:test';
import assert from 'node:assert/strict';
import {layoutGraph, NODE_W} from '../architecture-layout.js';

const n = id => ({id});
test('layers follow dependency direction and are deterministic', () => {
  const nodes = ['c', 'a', 'b'].map(n), edges = [{from: 'a', to: 'b'}, {from: 'b', to: 'c'}];
  const l = layoutGraph(nodes, edges), l2 = layoutGraph([...nodes].reverse(), [...edges].reverse());
  const x = id => l.nodes.find(v => v.id === id).x;
  assert.ok(x('a') < x('b') && x('b') < x('c'));
  assert.deepEqual(l, l2);
});
test('cycles terminate and the back edge is still drawn, flagged', () => {
  const l = layoutGraph(['a', 'b'].map(n), [{from: 'a', to: 'b'}, {from: 'b', to: 'a'}]);
  assert.equal(l.edges.length, 2);
  assert.equal(l.edges.filter(e => e.back).length, 1);
});
test('limit keeps the best connected nodes and reports the hidden count', () => {
  const nodes = ['hub', 'x', 'y', 'z'].map(n), edges = ['x', 'y', 'z'].map(t => ({from: 'hub', to: t}));
  const l = layoutGraph(nodes, edges, {limit: 2});
  assert.equal(l.nodes.length, 2); assert.equal(l.hidden, 2);
  assert.ok(l.nodes.some(v => v.id === 'hub'));
});
test('self loops and edges to hidden nodes are dropped; empty input is safe', () => {
  assert.equal(layoutGraph([n('a')], [{from: 'a', to: 'a'}, {from: 'a', to: 'ghost'}]).edges.length, 0);
  const e = layoutGraph([], []);
  assert.equal(e.nodes.length, 0); assert.ok(e.width > 0 && e.height > 0);
});
test('node width is exported for the renderer', () => assert.ok(NODE_W > 100));
