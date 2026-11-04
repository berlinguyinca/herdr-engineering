import test from 'node:test';
import assert from 'node:assert/strict';
import {reviewOutcome, reviewsNeedingDecision, decisionGaps, reviewTrend, renderReviewDecisions, renderReviewTrend, MAX_RECORDS} from './review-decisions.js';

const NOW = Date.parse('2026-10-30T12:00:00Z');
const ago = ms => new Date(NOW - ms).toISOString();
const H = 3600000, D = 24 * H;
const rec = (id, o = {}) => ({review_id: id, status: 'approved', created_at: ago(2 * D), completed_at: ago(D), ...o});

test('outcomes: a decision beats the status, and an unrecognised status is unknown, never a pass', () => {
  assert.equal(reviewOutcome({status: 'approved'}), 'approved');
  assert.equal(reviewOutcome({status: 'changes_requested'}), 'changes');
  assert.equal(reviewOutcome({status: 'closed', decision: 'annotated'}), 'changes');
  assert.equal(reviewOutcome({status: 'failed'}), 'failed');
  assert.equal(reviewOutcome({status: 'closed'}), 'closed');
  assert.equal(reviewOutcome({status: 'awaiting_review'}), 'open');
  assert.equal(reviewOutcome({status: 'done'}), 'unknown');
  assert.equal(reviewOutcome({status: 'done', verdict: 'approved'}), 'approved');
  for (const bad of [null, undefined, 7, 'x', [], {status: 5}, {}]) assert.equal(reviewOutcome(bad), 'unknown');
});

test('needs a decision: awaiting first, then stale, then unanswered; oldest first within a group', () => {
  const items = reviewsNeedingDecision([
    rec('c', {status: 'changes_requested', completed_at: ago(3 * D), created_at: ago(4 * D), pull_request_number: 1, repository: 'o/a'}),
    rec('s', {status: 'awaiting_review', stale: true, completed_at: null, created_at: ago(5 * H)}),
    rec('a2', {status: 'awaiting_review', completed_at: null, created_at: ago(1 * H)}),
    rec('a1', {status: 'awaiting_review', completed_at: null, created_at: ago(9 * H)}),
    rec('fin', {status: 'approved'}),
  ], NOW);
  assert.deepEqual(items.map(i => i.id), ['a1', 'a2', 's', 'c']);
  assert.deepEqual(items.map(i => i.kind), ['awaiting', 'awaiting', 'stale', 'unanswered']);
  assert.match(items[2].reason, /stopped reporting/);
});

test('a starting review is listed only when stale', () => {
  const ids = reviewsNeedingDecision([rec('fresh', {status: 'starting', completed_at: null}), rec('old', {status: 'starting', stale: true, completed_at: null})], NOW).map(i => i.id);
  assert.deepEqual(ids, ['old']);
});

test('changes requested: answered by a later review of the same PR, old requests are history, unknown targets are hedged', () => {
  const pr = {repository: 'o/a', pull_request_number: 5};
  const items = reviewsNeedingDecision([
    rec('asked', {...pr, status: 'changes_requested', created_at: ago(3 * D), completed_at: ago(2 * D)}),
    rec('answer', {...pr, status: 'approved', created_at: ago(D), completed_at: ago(D / 2)}),
    rec('ancient', {status: 'changes_requested', session_id: 'x', created_at: ago(40 * D), completed_at: ago(39 * D)}),
    rec('anon', {status: 'changes_requested', created_at: ago(2 * D), completed_at: ago(D)}),
    rec('undated', {status: 'changes_requested', created_at: null, completed_at: null, updated_at: null}),
  ], NOW);
  assert.deepEqual(items.map(i => [i.id, i.kind]), [['anon', 'unverified']]);
  assert.match(items[0].reason, /cannot be checked/);
});

test('bad input never throws and a huge list is bounded', () => {
  assert.deepEqual(reviewsNeedingDecision(null, NOW), []);
  assert.deepEqual(reviewsNeedingDecision([null, 3, 'x', [], {status: 7, stale: 'yes'}], NOW), []);
  const many = Array.from({length: MAX_RECORDS + 50}, (_, i) => rec(`r${i}`, {status: 'awaiting_review', completed_at: null}));
  assert.equal(reviewsNeedingDecision(many, NOW).length, MAX_RECORDS);
  assert.equal(reviewTrend(many, {now: NOW}).omitted, 50);
});

test('trend counts decided reviews per UTC day and keeps unknown outcomes out of pass and fail', () => {
  const m = reviewTrend([
    rec('1', {completed_at: '2026-10-29T10:00:00Z'}),
    rec('2', {completed_at: '2026-10-29T23:30:00Z', status: 'changes_requested'}),
    rec('3', {completed_at: '2026-10-28T01:00:00Z', status: 'failed'}),
    rec('4', {status: 'done'}),
    rec('5', {status: 'awaiting_review', completed_at: null}),
    rec('6', {status: 'approved', completed_at: null}),
    rec('7', {status: 'approved', completed_at: 'not a date'}),
    rec('8', {status: 'closed'}),
    rec('9', {completed_at: ago(60 * D)}),
  ], {period: 30, now: NOW});
  assert.equal(m.days.length, 30);
  assert.deepEqual(m.days.filter(d => d.approved + d.changes + d.failed), [{day: '2026-10-28', approved: 0, changes: 0, failed: 1}, {day: '2026-10-29', approved: 1, changes: 1, failed: 0}]);
  assert.equal(m.plotted, 3);
  assert.deepEqual([m.unknown, m.undecided, m.undated, m.closed, m.outside, m.total], [1, 1, 2, 1, 1, 9]);
});

test('trend periods: 7 days narrows, 90 days buckets by Monday-start UTC week, bad period falls back to 30', () => {
  const list = [rec('a', {completed_at: ago(2 * D)}), rec('b', {completed_at: ago(20 * D)}), rec('c', {completed_at: ago(80 * D)})];
  assert.equal(reviewTrend(list, {period: 7, now: NOW}).plotted, 1);
  assert.equal(reviewTrend(list, {period: 7, now: NOW}).days.length, 7);
  assert.equal(reviewTrend(list, {period: 7, now: NOW}).days.at(-1).day, '2026-10-30');
  assert.equal(reviewTrend(list, {period: 'x', now: NOW}).period, 30);
  const w = reviewTrend(list, {period: 90, now: NOW});
  assert.equal(w.bucket, 'week');
  assert.equal(w.plotted, 3);
  assert.equal(w.days.length, 13);                         // whole weeks: no partial first week
  for (const d of w.days) assert.equal(new Date(d.day + 'T00:00:00Z').getUTCDay(), 1);
});

test('rendering escapes everything, states the coverage and the empty state', () => {
  const evil = '<img src=x onerror=alert(1)>';
  const html = renderReviewDecisions([{id: evil, kind: 'awaiting', reason: evil, ageMs: 5 * H, record: {}}], () => '<i>row</i>');
  assert.ok(!html.includes('<img'));
  assert.match(html, /Waiting 5 h/);
  assert.match(renderReviewDecisions([], () => ''), /Nothing is waiting for a decision/);
  const trend = renderReviewTrend(reviewTrend([rec('1'), rec('2', {status: 'done'})], {now: NOW}));
  assert.match(trend, /1 of 2 loaded reviews are plotted; not plotted: 1 with no recorded outcome/);
  assert.match(trend, /<table/);
  assert.match(renderReviewTrend(reviewTrend([], {now: NOW})), /No review with a recorded outcome/);
});

test('answered: only a later COMPLETED review of the same kind and pull request answers', () => {
  const pr = {repository: 'o/a', pull_request_number: 5, kind: 'code'};
  const asked = rec('asked', {...pr, status: 'changes_requested', created_at: ago(3 * D), completed_at: ago(2 * D)});
  const ids = others => reviewsNeedingDecision([asked, ...others], NOW).map(i => [i.id, i.kind]);
  assert.deepEqual(ids([rec('f', {...pr, status: 'failed', created_at: ago(D)})]), [['asked', 'unanswered']]);
  assert.deepEqual(ids([rec('c', {...pr, status: 'closed', created_at: ago(D)})]), [['asked', 'unanswered']]);
  assert.deepEqual(ids([rec('o', {...pr, status: 'awaiting_review', completed_at: null, created_at: ago(H)})]).filter(([id]) => id === 'asked'), [['asked', 'rereview']]);
  assert.match(reviewsNeedingDecision([asked, rec('o', {...pr, status: 'awaiting_review', completed_at: null, created_at: ago(H)})], NOW).find(i => i.id === 'asked').reason, /later review .* in progress/);
  assert.deepEqual(ids([rec('ok', {...pr, status: 'approved', created_at: ago(D)})]), []);
  assert.deepEqual(ids([rec('again', {...pr, status: 'changes_requested', created_at: ago(D), completed_at: ago(H)})]).map(r => r[0]), ['again']);
});

test('answered: PR key first; session only when neither names a PR; plan and code never answer each other', () => {
  const a = rec('a', {repository: 'o/a', pull_request_number: 5, session_id: 'S', kind: 'code', status: 'changes_requested', created_at: ago(3 * D), completed_at: ago(2 * D)});
  const sessionOnly = rec('b', {session_id: 'S', kind: 'code', status: 'approved', created_at: ago(D)});
  assert.deepEqual(reviewsNeedingDecision([a, sessionOnly], NOW).map(i => i.id), ['a']);          // B has no PR: does not answer a PR review
  const otherPr = rec('c', {repository: 'o/a', pull_request_number: 6, session_id: 'S', kind: 'code', status: 'approved', created_at: ago(D)});
  assert.deepEqual(reviewsNeedingDecision([a, otherPr], NOW).map(i => i.id), ['a']);              // another PR by the same session
  const noPr = rec('d', {session_id: 'S', kind: 'code', status: 'changes_requested', created_at: ago(3 * D), completed_at: ago(2 * D)});
  assert.deepEqual(reviewsNeedingDecision([noPr, sessionOnly], NOW).map(i => i.id), []);          // neither names a PR: the session answers
  const plan = rec('e', {repository: 'o/a', pull_request_number: 5, kind: 'plan', status: 'approved', created_at: ago(D)});
  assert.deepEqual(reviewsNeedingDecision([a, plan], NOW).map(i => i.id), ['a']);                 // a plan review does not answer a code review
});

test('gaps: records past the bound and change requests without a time are reported, not silent', () => {
  const many = Array.from({length: MAX_RECORDS + 3}, (_, i) => rec(`r${i}`));
  assert.deepEqual(decisionGaps(many), {omitted: 3, undated: 0});
  assert.deepEqual(decisionGaps([rec('u', {status: 'changes_requested', created_at: null, completed_at: null, updated_at: null})]), {omitted: 0, undated: 1});
});

test('trend: future-dated records are counted, not plotted; a partial list says older outcomes are missing', () => {
  const m = reviewTrend([rec('f', {completed_at: new Date(NOW + 3 * D).toISOString()}), rec('ok', {completed_at: ago(H)})], {now: NOW});
  assert.equal(m.future, 1);
  assert.equal(m.plotted, 1);
  assert.match(renderReviewTrend(m), /1 future-dated/);
  const html = renderReviewTrend(m, {coverage: {hasMore: true, total: 900}});
  assert.match(html, /2 of 900 reviews are loaded; older outcomes are missing/);
  assert.ok(!/covers every review/.test(html));
  assert.match(renderReviewTrend(m), /covers every review the server returned/);
});

test('needs-decision never says nothing is waiting when reviews are missing', () => {
  const row = () => '';
  assert.match(renderReviewDecisions([], row, {coverage: {loaded: 2, total: 2}}), /data-rv-clear/);
  const partial = renderReviewDecisions([], row, {coverage: {hasMore: true, loaded: 500, total: 812, waitingKnown: true}});
  assert.ok(!partial.includes('data-rv-clear'));
  assert.ok(!/Nothing is waiting/.test(partial));
  assert.match(partial, /500 of 812 reviews are loaded; older reviews may have changes requested that nobody answered/);
  assert.match(renderReviewDecisions([], row, {coverage: {hasMore: true, loaded: 500, total: 812}}), /could not be read, so older waiting reviews may be missing/);
  assert.match(renderReviewDecisions([], row, {coverage: {loaded: 1, total: 1, gaps: {omitted: 4, undated: 2}}}), /4 further reviews were not examined.*2 reviews with requested changes have no usable time/);
  const some = renderReviewDecisions([{id: 'x', kind: 'awaiting', reason: 'r', ageMs: 0, record: {}}], row, {coverage: {hasMore: true, loaded: 500, total: 812, waitingKnown: true}});
  assert.match(some, /data-rv-item="x"/);
  assert.match(some, /data-rv-caveat/);
});
