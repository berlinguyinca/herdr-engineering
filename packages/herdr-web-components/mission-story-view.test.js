import test from 'node:test';
import assert from 'node:assert/strict';
import { mountMissionStory, renderStory } from '../mission-story-view.js';

const story = (over = {}) => ({
  header: { key: 'm1', title: 'Fix parser', stage: 'complete', result: 'succeeded', repository: 'org/app', branch: 'fix/p', hosts: ['beast'],
    started_at: '2026-10-24T10:00:00+00:00', ended_at: '2026-10-24T12:00:00+00:00', duration_s: 7200, agent_count: 2, tokens_out: 1200000,
    inferred: false, recorded_before_outcomes: false,
    summary: { pr: { number: 214, state: 'merged' }, ci: 'success', tests: { runs: 2, passed: 128, failed: 0 } } },
  needs: [], more: 0, partial: [],
  events: [
    { ts: '2026-10-24T10:00:00+00:00', kind: 'start', text: 'Mission started: Fix parser' },
    { ts: '2026-10-24T10:01:00+00:00', kind: 'agent', text: 'Started: Amber Swift (pi, qwen)', ref: { kind: 'session', id: 'herdr:beast:eng:p1' } },
    { ts: '2026-10-24T11:00:00+00:00', kind: 'wait', text: 'Waiting: approval of the plan', span_s: 840 },
    { ts: '2026-10-24T11:30:00+00:00', kind: 'pr', text: 'PR #214 merged', ref: { kind: 'pr', id: 'pr1', repository: 'org/app', number: 214 } },
    { ts: '2026-10-24T11:40:00+00:00', kind: 'ci', text: 'CI run #12 success on fix/p', ref: { kind: 'ci', id: '7:12', repository: 'org/app', run_number: 12, repo_filter: '7' } },
    { ts: '2026-10-24T12:00:00+00:00', kind: 'end', text: 'Ended: complete' },
  ],
  agents: [{ session_id: 'herdr:beast:eng:p1', label: 'Amber Swift', role: 'pi', model: 'qwen', started_at: '2026-10-24T10:01:00+00:00', ended_at: '2026-10-24T11:50:00+00:00', outcome: 'finished', summary: 'Fixed the flaky timing assertion.' }],
  outcome: { recorded: true, live: false, agent_count: 3, tests: { runs: 2, passed: 128, failed: 0 }, ci: [{ repository: 'org/app', branch: 'fix/p', run: 12, state: 'success' }],
    reviews: { open: 0, approved: 1, changes_requested: 0 }, prs: [{ repository: 'org/app', number: 214, state: 'merged' }] },
  usage: { requests: 40, input_tokens: 2000, output_tokens: 1200000, lower_bound: false }, objective: 'Make the parser faster', related: {},
  ...over,
});

test('a succeeded mission shows chip, one-line outcome, events in order, agents and cost', () => {
  const html = renderStory(story());
  assert.match(html, /data-tone="ok">Succeeded</);
  assert.match(html, /2h 0m · 2 agents · 1\.2 M output tokens/);
  assert.match(html, /PR #214 merged · CI passed · tests 128 passed/);
  assert.match(html, /org\/app · fix\/p · host beast/);
  assert.match(html, /Waiting: approval of the plan \(14m 0s\)/);
  assert.ok(html.indexOf('Mission started') < html.indexOf('Ended: complete'));
  assert.match(html, /href="#\/herd\/session\/herdr%3Ab%3Ae%3Ap1"|href="#\/herd\/session\/herdr%3Abeast%3Aeng%3Ap1"/);
  assert.match(html, /href="#\/ci\?search=12&amp;repository=7"/);
  assert.match(html, /href="https:\/\/github.com\/org\/app\/pull\/214" target="_blank" rel="noopener noreferrer"/);
  assert.match(html, /href="#\/llm\/session\/herdr%3Abeast%3Aeng%3Ap1">LLM usage</);
  assert.match(html, /Fixed the flaky timing assertion/);
  assert.match(html, /\+2 more agents/);
  assert.doesNotMatch(html, /Needs you now/);
});

test('failed, cancelled, running and unknown results each get their own chip', () => {
  for (const [result, text] of [['failed', 'Failed'], ['cancelled', 'Cancelled'], ['running', 'Running'], ['unknown', 'Ended, outcome unknown'], ['weird', 'Ended, outcome unknown']]) {
    assert.match(renderStory(story({ header: { ...story().header, result } })), new RegExp(`>${text}<`), result);
  }
});

test('unknown values are placeholders, never zero', () => {
  const html = renderStory(story({ header: { ...story().header, duration_s: null, tokens_out: null, agent_count: null, summary: {} },
    usage: null, outcome: { recorded: false, live: true, tests: null, ci: null, reviews: null, prs: null }, agents: null, partial: ['ci', 'related:x'] }));
  assert.match(html, /— · — agents/);
  assert.match(html, /Tests could not be read/);
  assert.match(html, /CI could not be read/);
  assert.match(html, /LLM usage could not be read/);
  assert.match(html, /Agents could not be read/);
  assert.match(html, /Could not read: ci\./);
  assert.doesNotMatch(html, /related:x|0 output tokens|0 requests/);
});

test('an inferred close and a pre-outcome mission are labelled', () => {
  assert.match(renderStory(story({ header: { ...story().header, inferred: true, result: 'unknown' } })), /Closed automatically after a long silence; how it ended is not known/);
  assert.match(renderStory(story({ header: { ...story().header, recorded_before_outcomes: true } })), /Recorded before outcomes were kept/);
});

test('needs show only when something is open and link only to dashboard routes', () => {
  const html = renderStory(story({ needs: [{ title: 'Agent failed', detail: 'reported failed', href: '#/herd/session/x' }, { title: 'Bad', detail: 'd', href: 'javascript:alert(1)' }] }));
  assert.match(html, /Needs you now/);
  assert.match(html, /href="#\/herd\/session\/x">Open/);
  assert.doesNotMatch(html, /javascript:/);
});

test('every dynamic string is escaped and refs never become urls from text', () => {
  const evil = '<img src=x onerror=1>';
  const html = renderStory(story({ objective: evil, header: { ...story().header, repository: evil, hosts: [evil] },
    events: [{ ts: '2026-10-24T10:00:00+00:00', kind: 'agent', text: evil, ref: { kind: 'session', id: '"><script>x' } }, { ts: '2026-10-24T10:01:00+00:00', kind: 'pr', text: evil, ref: { kind: 'pr', repository: 'javascript:alert(1)//x', number: 1 } }],
    agents: [{ session_id: 's"x', label: evil, role: evil, model: evil, outcome: evil, summary: evil }], outcome: { ...story().outcome, ci: [{ repository: evil, branch: evil, run: evil, state: evil }] } }));
  assert.doesNotMatch(html, /<img|<script/);
  assert.doesNotMatch(html, /href="javascript/);
  assert.match(html, /&lt;img/);
});

test('mountMissionStory renders, keeps focus-safe re-renders, stops on dispose and reports failure by leaving the page as it was', async () => {
  const el = { isConnected: true, innerHTML: 'x', replaceChildren() { this.innerHTML = ''; } };
  let calls = 0;
  const client = { missionStory: async () => { calls++; return story(); } };
  const handle = mountMissionStory(el, client, 'm1');
  assert.equal(await handle.ready, true);
  assert.match(el.innerHTML, /Mission story/);
  handle.dispose();
  const failing = { isConnected: true, innerHTML: 'old', replaceChildren() { this.innerHTML = ''; } };
  const h2 = mountMissionStory(failing, { missionStory: async () => { throw Object.assign(new Error('nf'), { status: 404 }); } }, 'x');
  assert.equal(await h2.ready, false);
  assert.equal(failing.innerHTML, '');
  assert.equal(calls, 1);
});
