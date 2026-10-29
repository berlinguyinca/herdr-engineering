import test from 'node:test';
import assert from 'node:assert/strict';
import { lineageModel } from '../agents-lineage.js';

const entry = (id, mission, created, updated, tab = 'current') => ({ id, host: 'h', role: id, tab, mission: mission ? { key: mission, title: mission } : null,
  record: { created_at: created, updated_at: updated, mission_id: mission } });

test('agents are laid out one lane per mission, newest activity first', () => {
  const m = lineageModel([entry('a', 'm1', '2026-10-28T10:00:00Z', '2026-10-28T10:30:00Z'), entry('b', 'm2', '2026-10-28T11:00:00Z', '2026-10-28T11:30:00Z'),
    entry('c', 'm1', '2026-10-28T10:40:00Z', '2026-10-28T10:50:00Z')], [], [], Date.parse('2026-10-28T12:00:00Z'));
  assert.deepEqual(m.lanes.map(l => l.key), ['m2', 'm1']);
  assert.equal(m.lanes[1].agents.length, 2);
  assert.equal(m.handoffs.length, 0);
});

test('a handoff is drawn only when both ends are shown, and node ids resolve to sessions', () => {
  const es = [entry('a', 'm1', '2026-10-28T10:00:00Z', '2026-10-28T10:30:00Z'), entry('b', 'm1', '2026-10-28T10:31:00Z', '2026-10-28T10:50:00Z')];
  const m = lineageModel(es, [{ from: 'n1', to: 'n2', state: 'claimed', goal: 'review' }, { from: 'n1', to: 'gone', state: 'queued' }],
    [{ id: 'n1', session_id: 'a' }, { id: 'n2', session_id: 'b' }], Date.parse('2026-10-28T12:00:00Z'));
  assert.deepEqual(m.handoffs.map(h => [h.from, h.to]), [['a', 'b']]);
});

test('more than eight missions are capped and counted', () => {
  const es = Array.from({ length: 11 }, (_, i) => entry(`s${i}`, `m${i}`, '2026-10-28T10:00:00Z', `2026-10-28T10:${10 + i}:00Z`));
  const m = lineageModel(es, [], [], Date.parse('2026-10-28T12:00:00Z'));
  assert.equal(m.lanes.length, 8); assert.equal(m.hidden, 3);
});

import { agentStory } from '../agents-lineage.js';

test('an agent story names how it started, its handoffs, and its last report', () => {
  const s = { session_id: 'a', host_id: 'beast', cwd: '/x/proj', created_at: '2026-10-28T10:00:00Z', updated_at: '2026-10-28T11:00:00Z', summary: 'Running tests' };
  const alone = agentStory(s, {});
  assert.deepEqual(alone.steps.map(x => x.kind), ['start', 'now']);
  assert.match(alone.steps[0].text, /Started on beast · proj/);
  const tree = { nodes: [{ id: 'n1', session_id: 'a' }, { id: 'n0', agent_id: 'planner' }, { id: 'n2', agent_id: 'reviewer' }],
    edges: [{ from: 'n0', to: 'n1', goal: 'implement', updated_at: '2026-10-28T10:00:00Z' }, { from: 'n1', to: 'n2', state: 'queued', goal: 'review', updated_at: '2026-10-28T10:30:00Z' }] };
  const t = agentStory(s, tree);
  assert.match(t.steps[0].text, /Started by handoff from planner: implement/);
  assert.match(t.steps[1].text, /Handed off \(queued\) to reviewer: review/);
  assert.equal(t.hasHandoff, true);
});
