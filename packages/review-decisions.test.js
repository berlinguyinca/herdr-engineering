import test from 'node:test';
import assert from 'node:assert/strict';
import {reviewOutcome, reviewsNeedingDecision, reviewTrend, renderReviewDecisions, renderReviewTrend, MAX_RECORDS} from './review-decisions.js';

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
  assert.deepEqual(m.days, [{day: '2026-10-28', approved: 0, changes: 0, failed: 1}, {day: '2026-10-29', approved: 1, changes: 1, failed: 0}]);
  assert.equal(m.plotted, 3);
  assert.deepEqual([m.unknown, m.undecided, m.undated, m.closed, m.outside, m.total], [1, 1, 2, 1, 1, 9]);
});

test('trend periods: 7 days narrows, 90 days buckets by Monday-start UTC week, bad period falls back to 30', () => {
  const list = [rec('a', {completed_at: ago(2 * D)}), rec('b', {completed_at: ago(20 * D)}), rec('c', {completed_at: ago(80 * D)})];
  assert.equal(reviewTrend(list, {period: 7, now: NOW}).plotted, 1);
  assert.equal(reviewTrend(list, {period: 'x', now: NOW}).period, 30);
  const w = reviewTrend(list, {period: 90, now: NOW});
  assert.equal(w.bucket, 'week');
  assert.equal(w.plotted, 3);
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
