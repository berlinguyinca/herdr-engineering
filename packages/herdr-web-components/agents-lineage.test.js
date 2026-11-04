import test from 'node:test';
import assert from 'node:assert/strict';
import { lineageModel, commitSteps } from '../agents-lineage.js';

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

import { renderStory } from '../agents-lineage.js';

test('the story lists the commits the server linked, or says plainly that none is recorded', () => {
  const s = { session_id: 'a', created_at: '2026-10-28T10:00:00Z', updated_at: '2026-10-28T11:00:00Z' };
  const linked = agentStory(s, {}, Date.now(), { state: 'linked', commits: [
    { sha: 'abcdef1234567', run_number: 9, state: 'success', pull_request: 42, at: '2026-10-28T10:30:00Z', ci_url: 'https://ci.example/r/9' },
    { sha: 'f'.repeat(40), run_number: 8, state: 'failure', match: 'commit', at: '2026-10-28T10:20:00Z', ci_url: 'javascript:alert(1)' }] });
  assert.deepEqual(linked.steps.map(x => x.kind), ['start', 'commit', 'commit', 'now']);
  assert.match(linked.steps[1].text, /Commit fffffff · CI run #8 failure \(the commit the agent reported\)/);
  assert.match(linked.steps[2].text, /Commit abcdef1 · PR #42 · CI run #9 success/);
  const html = renderStory(linked);
  assert.match(html, /<a href="https:\/\/ci\.example\/r\/9"/);
  assert.doesNotMatch(html, /javascript:/);
  for (const state of ['none', 'ambiguous']) {
    const out = agentStory(s, {}, Date.now(), { state, commits: [], reason: 'main is a shared branch <b>' });
    const note = out.steps.find(x => x.kind === 'unlinked');
    assert.match(note.text, /^No commit link recorded: main is a shared branch/);
    assert.doesNotMatch(renderStory(out), /<b>/);
  }
  const prOnly = agentStory(s, {}, Date.now(), { state: 'linked', commits: [], pull_requests: [{ number: 7, status: 'open', url: 'https://github.com/o/r/pull/7', at: '2026-10-28T10:40:00Z' }] });
  assert.deepEqual(prOnly.steps.map(x => x.text).slice(1, 2), ['PR #7 open']);
  assert.deepEqual(agentStory(s, {}, Date.now(), null).steps.map(x => x.kind), ['start', 'now']);
});

test('commit lines say when they are loading or failed, and untimed lines sit before the last report',()=>{
 const session={session_id:'s',created_at:'2026-10-29T12:00:00Z',updated_at:'2026-10-29T13:00:00Z',status:'finished'};
 assert.match(agentStory(session,{},Date.now(),'loading').steps.map(x=>x.text).join('|'),/Checking which commits/);
 assert.match(agentStory(session,{},Date.now(),'error').steps.map(x=>x.text).join('|'),/could not be loaded/);
 const steps=agentStory(session,{},Date.now(),{state:'none',reason:'nothing in the window'}).steps;
 assert.equal(steps.at(-1).kind,'now');
 assert.match(steps.at(-2).text,/No commit link recorded: nothing in the window/);
 const many=commitSteps({state:'linked',commits:[{sha:'a'.repeat(40),run_number:1,state:'success',at:'2026-10-29T12:10:00Z'}],omitted:10,pull_requests:[]});
 assert.match(many.map(x=>x.text).join('|'),/10 more commits not listed/);
});
