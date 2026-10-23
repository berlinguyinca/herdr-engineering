import test from 'node:test';
import assert from 'node:assert/strict';
import { ACTION_LABEL, freshDraft, proposalBody, renderForks, renderSteering, renderWorkbench } from '../collab-workbench.js';

const HOSTILE = '<img src=x onerror="window.__xss=1">"onmouseover="x\'><script>alert(1)</script>';
const H0 = 'a'.repeat(40);
const H1 = 'b'.repeat(40);
const card = (extra = {}) => ({ approach_id: 'approach:f1', label: 'try a queue', state: 'running', superseded: false, selected: false, branch: 'pi-fork/f1', base_sha: H0, head_sha: H1,
  provenance: { source: 'approach_owner', writer: 'key:writer', verified: false },
  diff: { files_changed: 2, insertions: 10, deletions: 1 }, impact: { modules: ['docs', 'src'], touched_paths: ['src/a.py'] },
  evidence: { counted_validations: 1, counted_reviews: 0, blocking_reviews: 0 }, readiness: { ready: true, blockers: [] }, ...extra });
const forks = (cards = [card()], extra = {}) => ({ origin: { id: 'attempt:a1' }, forks: cards, truncated: false, overlap: [], downstream: { per_fork: [] },
  quorum: { status: 'unavailable' }, can_propose: true, can_confirm: false, ...extra });
const target = (extra = {}) => ({ node_id: 'attempt:a1', node_type: 'attempt', state: 'done', superseded: false, fence: 'f'.repeat(64), pinned_source: 'key:abc', steering_enabled: true,
  actions: ['fork'], can_propose: true, can_confirm: true, self_confirm: false, fork: { origin: 'attempt:a1', existing_forks: 1, max_forks_per_origin: 5, base_candidates: { [H0]: 'origin.head_sha' } }, ...extra });
const model = (extra = {}) => ({ nodeId: 'attempt:a1', comments: [], target: target(), steers: [], forks: forks(), me: 'bob', isOwner: false, writable: true, draft: freshDraft(),
  editing: null, history: null, confirming: null, busy: false, error: null, steerError: null, notice: null, ...extra });
const noRaw = (html) => {
  assert.ok(!html.includes('<img') && !html.includes('<script'), 'raw markup');
  assert.ok(!/"\s*on[a-z]+=/i.test(html), 'attribute breakout');
};

test('the comparison labels the fork-reported numbers as unverified and shows trusted evidence separately', () => {
  const html = renderForks(model());
  for (const needle of ['try a queue', 'approach:f1', 'not verified', 'key:writer', '1 trusted validation', 'ready to promote', 'Propose promotion', 'Review quorum is not available']) assert.ok(html.includes(needle), needle);
  assert.ok(html.includes('<th scope="col">Diff (self-reported)</th>'));
});

test('a fork that is not ready lists every blocker and offers no promote button', () => {
  const html = renderForks(model({ forks: forks([card({ readiness: { ready: false, blockers: [{ code: 'no_validation', message: 'no trusted validation is bound to the approach head' }] } })]) }));
  assert.ok(html.includes('not ready') && html.includes('no trusted validation'));
  assert.ok(!html.includes('promote-start'));
});

test('read-only readers, selected and superseded forks never get a promote button', () => {
  assert.ok(!renderForks(model({ writable: false })).includes('promote-start'));
  assert.ok(!renderForks(model({ forks: forks([card({ selected: true })]) })).includes('promote-start'));
  assert.ok(!renderForks(model({ forks: forks([card({ superseded: true })]) })).includes('promote-start'));
  assert.ok(!renderForks(model({ forks: forks([card()], { can_propose: false }) })).includes('promote-start'));
});

test('hostile labels, ids, branches, paths and blockers are escaped', () => {
  const c = card({ approach_id: HOSTILE, label: HOSTILE, branch: HOSTILE, base_sha: HOSTILE, head_sha: HOSTILE, state: HOSTILE,
    provenance: { writer: HOSTILE }, impact: { modules: [HOSTILE] }, readiness: { ready: false, blockers: [{ code: HOSTILE, message: HOSTILE }] } });
  const html = renderWorkbench(model({ nodeId: HOSTILE, forks: forks([c], { origin: { id: HOSTILE }, overlap: [{ a: HOSTILE, b: HOSTILE, paths: [HOSTILE] }],
    downstream: { per_fork: [{ approach_id: HOSTILE, evidence_left_on_other_head: [{}] }] } }), target: target({ node_id: HOSTILE }) }));
  noRaw(html);
  assert.ok(html.includes('&lt;img'));
});

test('the fork form offers only commits the server listed, and no branch or path field exists', () => {
  const html = renderSteering(model({ draft: { ...freshDraft(), action: 'fork', base_sha: H0 } }));
  assert.ok(html.includes('name="base_sha"') && html.includes(H0.slice(0, 12)) && html.includes('name="label"'));
  assert.ok(!/name="(branch|path|dir|command)"/.test(html));
  assert.ok(html.includes('never by this form'));
});

test('the promote form restates the exact head and warns when it is not ready', () => {
  const m = model({ nodeId: 'approach:f1', target: target({ node_id: 'approach:f1', node_type: 'approach', actions: ['promote'] }), draft: { ...freshDraft(), action: 'promote', expected_head: H1 } });
  const html = renderSteering(m);
  assert.ok(html.includes(H1) && html.includes('If the fork moves, the request is cancelled'));
  const bad = renderSteering({ ...m, forks: forks([card({ readiness: { ready: false, blockers: [] } })]) });
  assert.ok(bad.includes('not ready'));
});

test('request bodies for fork and promote carry only the typed fields', () => {
  const f = proposalBody('attempt:a1', { ...freshDraft(), action: 'fork', base_sha: ` ${H0} `, label: ' alt ' }, target(), 'k');
  assert.deepEqual(f, { action: 'fork', node_id: 'attempt:a1', expected_fence: 'f'.repeat(64), idempotency_key: 'k', base_sha: H0, label: 'alt' });
  const p = proposalBody('approach:f1', { ...freshDraft(), action: 'promote', expected_head: H1 }, target({ actions: ['promote'] }), 'k');
  assert.deepEqual(Object.keys(p).sort(), ['action', 'expected_fence', 'expected_head', 'idempotency_key', 'node_id']);
  for (const b of [f, p]) for (const forbidden of ['branch', 'path', 'worktree', 'command', 'argv', 'force', 'merge', 'room_id', 'owner']) assert.ok(!(forbidden in b));
  assert.ok(ACTION_LABEL.fork && ACTION_LABEL.promote);
});

test('a confirm summary shows the commit or head and the origin', () => {
  const m = model({ me: 'alice', confirming: 's1', target: target({ can_confirm: true }), steers: [{ steering_id: 's1', action: 'promote', status: 'pending', digest: 'd'.repeat(64), proposed_by: { id: 'bob' },
    params: { expected_head: H1, origin: 'attempt:a1' }, target: { node_id: 'approach:f1', node_type: 'approach', fence: 'f', pinned_source: 'key:abc', expected_digest: null } }] });
  const html = renderSteering(m);
  assert.ok(html.includes('Promote exactly this head') && html.includes(H1) && html.includes('attempt:a1'));
});

test('without a comparison nothing about forks is drawn, and a load error is announced', () => {
  assert.equal(renderForks(model({ forks: null })), '');
  assert.ok(renderForks(model({ forks: null, forksError: 'Forks could not be loaded for this item.' })).includes('role="alert"'));
});
