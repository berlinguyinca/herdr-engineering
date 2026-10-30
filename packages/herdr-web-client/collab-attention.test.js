import test from 'node:test';
import assert from 'node:assert/strict';
import { ackBody, duration, renderAnalytics, renderEstimate, renderInbox, renderProgress, safeHref, snoozeBody } from '../collab-attention.js';

const XSS = '<img src=x onerror=alert(1)>';
const item = (o = {}) => ({ key: 'ci_failed|ci:1', category: 'ci_failed', severity: 'error', title: 'CI failed', subject: { node_id: 'ci:1', label: 'ci' },
  fp: 'a'.repeat(24), state: 'open', for_you: false, detail: null, ...o });

test('severity is written as text, items have named actions, and the list is labelled', () => {
  const h = renderInbox({ data: { items: [item(), item({ key: 'blocked|task:1', severity: 'notice', title: 'Blocked' })], counts: { by_severity: { error: 1, notice: 1 } } } });
  assert.match(h, /<b>Error<\/b>/);
  assert.match(h, /<b>Notice<\/b>/);
  assert.match(h, /aria-label="Items that need attention"/);
  assert.match(h, /aria-label="Acknowledge: ci"/);
  assert.match(h, /<select data-attn-snooze/);
  assert.match(h, /role="status"/);
});

test('empty inbox, loading and errors are text', () => {
  assert.match(renderInbox({ data: { items: [], counts: {} } }), /Nothing needs your attention/);
  assert.match(renderInbox({}), /Loading/);
  assert.match(renderInbox({ error: 'nope' }), /role="alert"/);
});

test('untrusted labels, details and keys are escaped everywhere', () => {
  const h = renderInbox({ data: { items: [item({ key: `x"><script>`, subject: { label: XSS }, title: XSS, detail: { why: XSS } })], counts: {} } });
  assert.ok(!h.includes('<img') && !h.includes('<script'));
  assert.match(h, /&lt;img/);
  const p = renderProgress({ data: { room: { status: 'ok', tasks: 1, complete: 0, completion: 0, progress: 0 }, tasks: [{ task_id: 't', label: XSS, stage: 'none',
    evidence: [{ kind: 'merged_pr', node_id: 'pr:x', label: XSS, url: 'javascript:alert(1)', counted: false, why_not: [XSS] }] }] } });
  assert.ok(!p.includes('<img') && !p.includes('javascript:'));
  const a = renderAnalytics({ data: { scope: { note: XSS, min_group_size: 5 }, state_mix: { [XSS]: 7 } } });
  assert.ok(!a.includes('<img'));
});

test('only real github pull request links become hrefs', () => {
  assert.equal(safeHref('https://github.com/o/r/pull/12'), 'https://github.com/o/r/pull/12');
  for (const bad of ['javascript:alert(1)', 'http://github.com/o/r/pull/1', 'https://evil.com/o/r/pull/1', 'https://github.com/o/r/pull/1"onmouseover="x', null, 5]) assert.equal(safeHref(bad), null);
  const h = renderProgress({ data: { room: { status: 'ok', tasks: 1, complete: 1, completion: 1, progress: 1 }, tasks: [{ task_id: 't', label: 'T', stage: 'complete',
    evidence: [{ kind: 'merged_pr', node_id: 'pr:g', label: 'PR 1', url: 'https://github.com/o/r/pull/1', counted: true, why_not: [] }] }] } });
  assert.match(h, /<a href="https:\/\/github.com\/o\/r\/pull\/1" rel="noopener noreferrer"/);
});

test('acknowledged items stay out of the list and can be shown again', () => {
  const quiet = item({ state: 'acknowledged' });
  const h = renderInbox({ showQuiet: true, data: { items: [], quiet_items: [quiet], counts: { acknowledged: 1, snoozed: 0 } } });
  assert.match(h, /data-attn-act="restore"/);
  assert.match(h, /aria-expanded="true"/);
  assert.match(renderInbox({ data: { items: [item({ resurfaced: true })], counts: {} } }), /Changed since you dismissed it/);
});

test('progress labels an estimate and says insufficient evidence instead of guessing', () => {
  const none = renderProgress({ data: { room: { status: 'ok', tasks: 3, complete: 0, completion: 0, progress: 0,
    estimate: { label: 'estimate', status: 'insufficient_evidence', reason: '0 task(s) with timed completion evidence' } }, tasks: [] } });
  assert.match(none, /insufficient evidence/i);
  assert.ok(!/likely has evidence/.test(none));
  const est = renderEstimate({ status: 'estimate', method: 'm1', confidence: 'low', confidence_reasons: ['only 5 completions'], n_completed: 5,
    interval_seconds: { p25: 60, p50: 3600, p75: 7200 }, finish: { earliest: 'a', likely: 'b', latest: 'c' }, assumptions: ['x'] });
  assert.match(est, /<b>Estimate<\/b>/);
  assert.match(est, /confidence low/);
  assert.match(renderProgress({ data: { room: { status: 'no_trusted_evidence_configured', note: 'none listed', tasks: 0 }, tasks: [] } }), /No evidence source is configured/);
});

test('reported-done tasks without evidence are called out, not counted', () => {
  const h = renderProgress({ data: { room: { status: 'ok', tasks: 1, complete: 0, completion: 0, progress: 0, reported_done_without_evidence: 1 },
    tasks: [{ task_id: 't', label: 'T', stage: 'none', reported_done: true, evidence: [] }] } });
  assert.match(h, /reported done without evidence/);
  assert.match(h, /No evidence/);
});

test('suppressed analytics say why and never show a number', () => {
  const h = renderAnalytics({ data: { scope: { min_group_size: 5 }, cycle_time: { suppressed: true }, rework: { rate: 0.25, of: 8, basis: 'b' },
    cost: { self_reported: true, reported: false }, state_mix: { suppressed: true } } });
  assert.match(h, /too few items/);
  assert.match(h, /25%/);
  assert.match(h, /Not reported/);
});

test('request bodies and durations', () => {
  assert.deepEqual(ackBody('k', 'f'), { key: 'k', fp: 'f' });
  assert.deepEqual(snoozeBody('k', 'f', '3600'), { key: 'k', fp: 'f', seconds: 3600 });
  assert.equal(duration(30), '30 s');
  assert.equal(duration(7200), '2 h');
  assert.equal(duration(null), 'n/a');
});

test('a failed action is announced next to the list and does not hide it', () => {
  const h = renderInbox({ notice: 'Too many changes; try again in a minute.', data: { items: [item()], counts: {} } });
  assert.match(h, /role="alert">Too many changes/);
  assert.match(h, /Items that need attention/);
});

test('a panel whose first load failed shows its error, never a loading skeleton', () => {
  for (const render of [renderInbox, renderProgress, renderAnalytics]) {
    const h = render({ data: null, error: 'Could not load this view. It will retry.' });
    assert.doesNotMatch(h, /data-skeleton/);
    assert.match(h, /Could not load this view/);
    assert.match(render({ data: null, error: null }), /data-skeleton/, 'still loading: skeleton');
  }
});

test('delivery metrics show unknown as unknown, never as zero, and escape every text field', () => {
  const h = renderAnalytics({ data: { scope: { min_group_size: 5 }, delivery: {
    accepted_changes: { count: 'unknown', reason: XSS }, gate_failures: { count: 3, basis: XSS },
    handoffs: { total: 4, outcomes: { success: 2, failed: 1, pending: 1 } }, review_findings: { total: 6, open: 2 } } } });
  assert.match(h, /Accepted changes<\/th><td>Unknown/);
  assert.match(h, /Gate failures<\/th><td>3/);
  assert.match(h, /4 total: 2 succeeded, 1 failed, 1 pending/);
  assert.match(h, /6 total, 2 open/);
  assert.ok(!h.includes('<img'));
  assert.equal(renderAnalytics({ data: { scope: { min_group_size: 5 } } }).includes('Accepted changes'), false);
});
