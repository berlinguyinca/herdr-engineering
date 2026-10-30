import test from 'node:test';
import assert from 'node:assert/strict';
import { renderTrail, signalCards, trailModel } from './pr-trail-view.js';
import { prTrailHref, entityHref } from './entity-links.js';
import { commitSteps, renderStory, agentStory } from './agents-lineage.js';

const trail = {
  pull_request: { number: 7, repository: 'org/app', status: 'merged', branch: 'feat/x', url: 'https://github.com/org/app/pull/7' },
  state: 'linked', reason: '', sources: { ci: 'ok', handoffs: 'ok', reviews: 'unavailable', plan_reviews: 'ok' }, omitted: { sessions: 2, handoffs: 0 },
  sessions: [{ session_id: 's<1>', name: 'Coder', runtime: 'pi', rule: 'branch_window', started_at: '2026-10-29T12:00:00Z' }],
  ambiguous: [{ session_id: 'amb-1', reason: 'overlap' }],
  timeline: [
    { at: '2026-10-29T12:00:00Z', kind: 'session', ref: 's1', rule: 'branch_window', text: '<b>x</b> started' },
    { at: '2026-10-29T12:05:00Z', kind: 'handoff', ref: 'h1', rule: 'session', text: 'Handoff pi → claude: do it', state: 'rejected', problem: true },
    { at: '2026-10-29T12:10:00Z', kind: 'ci', ref: '7:1', rule: 'pull_request', text: 'CI run 1 success', url: 'javascript:alert(1)' },
  ],
  signals: {
    merged_without_rework: { value: null, reason: 'Review records could not be read.' }, ci_green_first_try: { value: true, reason: 'ok' },
    ci_reruns: { value: 0, reason: '' }, cost_usd_estimate: { value: 1.5, reason: '' }, duration_s: { value: 6000, reason: '' },
    handoff_problems: { value: 1, reason: '' }, review_verdicts: { value: [{ review_id: 'r', verdict: 'approved' }], reason: '' },
  },
};

test('an unknown signal says unknown with its reason, and a known false is not unknown', () => {
  const cards = signalCards({ merged_without_rework: { value: null, reason: 'why' }, ci_green_first_try: { value: false, reason: '' } });
  assert.deepEqual(cards.find((c) => c.key === 'merged_without_rework'), { key: 'merged_without_rework', label: 'Merged without rework', known: false, value: 'unknown', reason: 'why' });
  assert.equal(cards.find((c) => c.key === 'ci_green_first_try').value, 'no');
  assert.equal(cards.find((c) => c.key === 'cost_usd_estimate').value, 'unknown');
});

test('renders signals, unread sources, ambiguity and omitted counts; text is escaped and only https links are made', () => {
  const html = renderTrail(trail);
  assert.match(html, /data-signal="merged_without_rework" data-known="false"><b>unknown<\/b>/);
  assert.match(html, /\$1\.50/);
  assert.match(html, /reviews: unavailable/);
  assert.match(html, /data-pt-ambiguous[^>]*>[^<]*amb-1/);
  assert.match(html, /2 more sessions/);
  assert.match(html, /&lt;b&gt;x&lt;\/b&gt; started/);
  assert.match(html, /data-problem="true"/);
  assert.doesNotMatch(html, /javascript:/);
  assert.match(html, /href="#\/herd\/session\/s%3C1%3E"/);
});

test('an unlinked trail explains why instead of listing agents', () => {
  const html = renderTrail({ ...trail, state: 'none', reason: 'No agent session matches.', sessions: [], timeline: [] });
  assert.match(html, /data-pt-state[^>]*>No agent session matches\./);
  assert.doesNotMatch(html, /<h2>Agents<\/h2>/);
});

test('an odd body does not throw', () => {
  for (const body of [null, 'x', 5, [], { sessions: 'no', timeline: {}, signals: 3 }]) assert.doesNotThrow(() => renderTrail(body));
  assert.equal(trailModel(null).state, 'unknown');
});

test('pull request links point at the trail page', () => {
  assert.equal(prTrailHref('github.com/org/app', 7), '#/pr/org/app/7?host=github.com');
  assert.equal(prTrailHref('org/app', 7), '#/pr/org/app/7');
  assert.equal(prTrailHref('https://github.com/org/app.git', 7), '#/pr/org/app/7?host=github.com');
  assert.equal(prTrailHref('app', 7), null);
  assert.equal(prTrailHref('org/app', 0), null);
  assert.equal(entityHref('pr', 'org/app#12'), '#/pr/org/app/12');
  assert.match(entityHref('pr', 'some-id'), /^#\/reviews/);
});

test('the agent story links a pull request line to its trail', () => {
  const steps = commitSteps({ state: 'linked', repository_id: 'github.com/org/app', commits: [{ sha: 'a'.repeat(40), pull_request: 7, run_number: 1, state: 'success' }], pull_requests: [{ number: 7 }, { number: 9, status: 'open' }] });
  assert.deepEqual(steps.map((s) => s.trail), ['#/pr/org/app/7?host=github.com', '#/pr/org/app/9?host=github.com']);
  const html = renderStory(agentStory({ session_id: 's' }, {}, Date.now(), { state: 'linked', repository_id: 'org/app', commits: [], pull_requests: [{ number: 9 }] }));
  assert.match(html, /<a href="#\/pr\/org\/app\/9" data-pr-trail>how it got here<\/a>/);
});

test('a sub-cent cost is never shown as $0.00, and inferred agents are labelled as hints', () => {
  const cost = (value) => signalCards({ cost_usd_estimate: { value, reason: '' } }).find((c) => c.key === 'cost_usd_estimate').value;
  assert.equal(cost(0.004), '<$0.01');
  assert.equal(cost(0), '$0.00');
  assert.equal(cost(1.5), '$1.50');
  const html = renderTrail({ ...trail, inferred_sessions: [{ session_id: 'm1', name: 'Mission mate', rule: 'mission', inferred: true }] });
  assert.match(html, /<h2>Possibly involved<\/h2>/);
  assert.match(html, /data-inferred="true"/);
  assert.match(html, /not counted in any figure/);
});

test('link targets in the timeline are at least 44px tall', () => {
  assert.match(renderTrail(trail), /\.pt-line a\{[^}]*min-height:44px/);
});

test('a handoff on the trail links to its detail, with the id encoded', async () => {
  const { renderTrail } = await import('./pr-trail-view.js');
  const html = renderTrail({ pull_request: { number: 1, repository: 'o/r', status: 'open' }, state: 'linked',
    timeline: [{ at: '2026-01-01T00:00:00Z', kind: 'handoff', ref: 'h/1', text: 'pi → pi: go', state: 'queued' }] });
  assert.match(html, /href="#\/handoffs\?open=h%2F1"/);
});
