import test from 'node:test';
import assert from 'node:assert/strict';
import { ciAttentionModel, ciRefLabel, failureExcerpt, renderCiAttention, renderCiDetail, STUCK_RUNNING_MS } from '../ci-attention.js';

const NOW = Date.parse('2026-10-29T12:00:00Z');
const iso = (minAgo) => new Date(NOW - minAgo * 60000).toISOString();
const run = (over) => ({ repository: 'o/r', run_number: 1, state: 'success', branch: 'main', ref: 'refs/heads/main', event: 'push', created_at: iso(60), finished_at: iso(50), url: 'https://ci/x', ...over });
const model = (runs) => ciAttentionModel({ pipelines: { 'o/r': runs } }, NOW);

test('a failure is unresolved only while the newest finished run of its branch failed', () => {
  assert.equal(model([run({ run_number: 2, state: 'failure' }), run({ run_number: 1, state: 'success', created_at: iso(120), finished_at: iso(110) })]).failures.length, 1);
  assert.equal(model([run({ run_number: 3, state: 'success' }), run({ run_number: 2, state: 'failure', created_at: iso(120), finished_at: iso(110) })]).failures.length, 0, 'a later green run clears it');
  assert.equal(model([run({ run_number: 2, state: 'canceled' }), run({ run_number: 1, state: 'failure', created_at: iso(120), finished_at: iso(110) })]).failures.length, 1, 'a canceled run says nothing about the code');
});

test('branches and pull requests are separate: a green main does not clear a red PR', () => {
  const m = model([run({ run_number: 5, state: 'success' }), run({ run_number: 4, state: 'failure', event: 'pull_request', ref: 'refs/pull/9/merge', created_at: iso(30), finished_at: iso(20) })]);
  assert.equal(m.failures.length, 1);
  assert.equal(m.failures[0].label, 'PR #9');
});

test('consecutive failures are counted and a newer active run marks it re-running', () => {
  const m = model([run({ run_number: 4, state: 'running', created_at: iso(5), started_at: iso(4), finished_at: null }),
    run({ run_number: 3, state: 'failure', created_at: iso(40), finished_at: iso(30) }), run({ run_number: 2, state: 'failure', created_at: iso(90), finished_at: iso(80) }),
    run({ run_number: 1, state: 'success', created_at: iso(200), finished_at: iso(190) })]);
  assert.equal(m.failures[0].consecutive, 2);
  assert.equal(m.failures[0].rerunning, true);
});

test('failures older than 14 days and failures on merged pull requests are left out', () => {
  const old = model([run({ state: 'failure', created_at: iso(15 * 24 * 60), finished_at: iso(15 * 24 * 60) })]);
  assert.equal(old.failures.length, 0);
  const merged = model([run({ state: 'failure', pr_merged: true })]);
  assert.equal(merged.failures.length, 0);
  assert.equal(merged.merged, 1);
});

test('running runs past the limit are flagged stuck and listed first', () => {
  const m = ciAttentionModel({ pipelines: { a: [run({ repository: 'a', state: 'running', started_at: iso(10), created_at: iso(12) })], b: [run({ repository: 'b', state: 'running', started_at: new Date(NOW - STUCK_RUNNING_MS - 60000).toISOString(), created_at: iso(70) })] } }, NOW);
  assert.equal(m.running.length, 2);
  assert.equal(m.stuck, 1);
  assert.equal(m.running[0].run.repository, 'b');
});

test('a blocked run is aged but never called stuck', () => {
  const m = model([run({ state: 'blocked', created_at: iso(5000), started_at: null, finished_at: null })]);
  assert.equal(m.running[0].blocked, true);
  assert.equal(m.stuck, 0);
});

test('failureExcerpt keeps the lines around failure markers and falls back to the tail', () => {
  const log = ['setup', ...Array.from({ length: 30 }, (_, i) => `ok ${i}`), 'not ok 31 - thing broke', '  expected 1', '  actual 2', 'done'].join('\n');
  const ex = failureExcerpt(log);
  assert.equal(ex.matched, true);
  assert.ok(ex.lines.includes('not ok 31 - thing broke'));
  assert.ok(!ex.lines.includes('setup'));
  const none = failureExcerpt(Array.from({ length: 50 }, (_, i) => `line ${i}`).join('\n'));
  assert.equal(none.matched, false);
  assert.equal(none.lines.at(-1), 'line 49');
});

test('rendering escapes untrusted text and says when there is nothing to fix', () => {
  const m = model([run({ repository: '<img src=x onerror=1>', state: 'failure' })]);
  const html = renderCiAttention(m);
  assert.ok(!html.includes('<img src=x'));
  assert.match(html, /data-cia-row/);
  assert.match(renderCiAttention(model([run()])), /data-cia-clear/);
  assert.equal(ciRefLabel({ ref: 'refs/pull/12/merge', branch: 'main' }), 'PR #12');
});

test('the opened row shows the commit, failing steps, log excerpt, and says when no agent is linked', () => {
  const f = model([run({ state: 'failure', revision: 'abcdef1234567890', forge_url: 'https://github.com/o/r/pull/9' })]).failures[0];
  const detail = { pipeline: { author: 'sam', message: 'Fix the thing\nmore' }, diagnosis: { failed_steps: [{ id: 7, name: 'unit-gates', workflow: 'unit-gates', state: 'failure', exit_code: 1 }] } };
  const html = renderCiDetail(f, detail, { 7: { log: 'a\nnot ok 1 - boom\nb', truncated: true } });
  assert.match(html, /sam · Fix the thing/);
  assert.match(html, /unit-gates \/ unit-gates/);
  assert.match(html, /not ok 1 - boom/);
  assert.match(html, /stored log is truncated/);
  assert.match(html, /no agent session is linked/);
  assert.match(renderCiDetail(f, null, {}), /Loading the failing step/);
});
