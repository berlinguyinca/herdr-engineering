import test from 'node:test';
import assert from 'node:assert/strict';
import { handoffGraphSummary } from './handoffs-view.js';

test('summarizes handoff lineage without trusting goal text as markup', () => {
  const summary = handoffGraphSummary({
    nodes: [{ id: 'a', harness: 'pi' }, { id: 'b', harness: 'claude' }],
    edges: [{ handoff_id: 'h1', from: 'a', to: 'b', state: 'queued', goal: '<script>' }],
  });
  assert.equal(summary.nodes, 2);
  assert.equal(summary.edges, 1);
  assert.equal(summary.states.queued, 1);
  assert.equal(summary.edgeText, 'h1 · queued');
});
