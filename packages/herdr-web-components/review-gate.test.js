import test from 'node:test';
import assert from 'node:assert/strict';
import {
  shortSha, resumePath, stateLabel, renderGateSummary, renderMainStatus,
  renderPrTable, renderPrDetail, renderQueue, renderNotices,
} from '../review-gate-view.js';

const EVIL = '<img src=x onerror=alert(1)>';
const noMarkup = (html) => assert.doesNotMatch(html, /<img|<script/);

test('shortSha clips to 12 and tolerates junk', () => {
  assert.equal(shortSha('0123456789abcdef0123'), '0123456789ab');
  assert.equal(shortSha('abc'), 'abc');
  for (const v of [null, undefined, 42, {}, []]) assert.equal(shortSha(v), '');
});

test('resumePath builds the route and refuses hostile keys', () => {
  assert.equal(resumePath('github.com/o/n#12'), '/api/v1/review-gate/prs/github.com/o/n/12/resume');
  assert.equal(resumePath('github.com/a.b-c_d/e.f#7'), '/api/v1/review-gate/prs/github.com/a.b-c_d/e.f/7/resume');
  for (const k of ['github.com/o/../n#1', 'github.com/o/n x#1', 'github.com/o/<n>#1', 'github.com/o/n#', 'github.com/o/n',
    'gitlab.com/o/n#1', 'github.com/o/n#1\n', 'github.com/o/n#1/extra', null, undefined, 5]) {
    assert.equal(resumePath(k), null, String(k));
  }
});

test('stateLabel covers every state and escapes unknown', () => {
  for (const s of ['ci_pending', 'ci_green', 'review_queued', 'reviewing', 'changes_requested', 'approved', 'merged', 'ci_red', 'superseded', 'escalated']) {
    const label = stateLabel(s);
    assert.ok(label && !label.includes('_'), s);
  }
  assert.equal(stateLabel('escalated'), 'Escalated');
  assert.equal(stateLabel(EVIL), 'Unknown state');
  assert.equal(stateLabel('constructor'), 'Unknown state');
  assert.equal(stateLabel(null), 'Unknown');
});

test('renderPrTable escapes hostile key/reason and shows rounds', () => {
  const html = renderPrTable([{ key: `github.com/o/n#1${EVIL}`, head_sha: 'f'.repeat(40), state: 'escalated', review_rounds: 2, ci_fix_rounds: 3, reason: EVIL }], {});
  noMarkup(html);
  assert.match(html, /<th/);
  assert.match(html, /&lt;img/);
  assert.match(html, /review 2/);
  assert.match(html, /ci fix 3/);
  assert.match(html, /f{12}(?!f)/);
  assert.doesNotMatch(html, /f{13}/);
  assert.match(renderPrTable([], {}), /No pull requests/);
});

test('renderPrTable marks the selected row and shows the review job state', () => {
  const html = renderPrTable([{ key: 'github.com/o/n#1', state: 'reviewing' }], { 'github.com/o/n#1': { state: 'claimed' } }, 'github.com/o/n#1');
  assert.match(html, /aria-pressed="true"/);
  assert.match(html, /claimed/);
});

test('renderPrDetail lists findings escaped and clipped', () => {
  const html = renderPrDetail({ key: 'github.com/o/n#1', state: 'changes_requested', last_findings: [
    { path: EVIL, line: 3, severity: 'high', message: EVIL + 'x'.repeat(5000), required_change: EVIL },
  ] });
  noMarkup(html);
  assert.match(html, /&lt;img/);
  assert.ok(html.length < 4000);
});

test('renderPrDetail shows resume only for escalated', () => {
  const esc = renderPrDetail({ key: 'github.com/o/n#1', state: 'escalated', reason: 'repeated finding' });
  assert.match(esc, /data-rg-resume="github.com\/o\/n#1"/);
  assert.match(esc, /repeated finding/);
  assert.doesNotMatch(renderPrDetail({ key: 'github.com/o/n#1', state: 'approved' }), /data-rg-resume/);
  assert.doesNotMatch(renderPrDetail({ key: 'github.com/o/../n#1', state: 'escalated' }), /data-rg-resume/);
  assert.match(renderPrDetail({ key: 'github.com/o/n#1', state: 'escalated' }, { busy: true }), /data-rg-resume[^>]*disabled/);
  const err = renderPrDetail({ key: 'github.com/o/n#1', state: 'escalated' }, { error: EVIL });
  noMarkup(err);
  assert.match(err, /role="status"/);
});

test('renderMainStatus shows state, last green, failures and culprit PR numbers', () => {
  const html = renderMainStatus({ repos: [{ repo: 'o/n', branch: 'main', head_sha: 'a'.repeat(40), state: 'red', last_green_sha: 'b'.repeat(40), failures: 2, range_total: 5, culprits: [11, 12] }] });
  assert.match(html, /o\/n/);
  assert.match(html, /red/i);
  assert.match(html, /b{12}/);
  assert.match(html, /#11/);
  assert.match(html, /#12/);
  assert.match(html, /2/);
  noMarkup(renderMainStatus({ repos: [{ repo: EVIL, state: EVIL, culprits: [EVIL] }] }));
});

test('renderMainStatus tolerates missing fields', () => {
  assert.match(renderMainStatus({ repos: [{}] }), /<table|<li|<article/);
  assert.match(renderMainStatus({ repos: [] }), /No repositories/);
});

test('renderQueue shows depth, claimed vs queued, gate last error', () => {
  const html = renderQueue({ depth: 3, jobs: [
    { job_id: 'j1', key: 'github.com/o/n#1', sha: 'c'.repeat(40), author_model: 'm', enqueued_at: 1700000000, state: 'claimed' },
    { job_id: 'j2', key: 'github.com/o/n#2', sha: 'd'.repeat(40), state: 'queued' },
  ], gate: { last_tick_at: 1700000000, counts: { escalated: 1 }, last_error: EVIL } });
  noMarkup(html);
  assert.match(html, /1 claimed/);
  assert.match(html, /1 queued/);
  assert.match(html, /Depth 3/);
  assert.match(html, /&lt;img/);
  assert.match(renderGateSummary({ gate: { counts: { escalated: 2, approved: 1 } } }), /2<\/strong><span>Escalated/);
});

test('renderNotices shows channel and ok flag', () => {
  const html = renderNotices([{ key: 'github.com/o/n#1', sha: 'e'.repeat(40), kind: 'escalated', channel: 'user', ok: false, at: 1700000000 }, { key: EVIL, kind: EVIL, channel: EVIL, ok: true }]);
  noMarkup(html);
  assert.match(html, /user/);
  assert.match(html, /failed/i);
  assert.match(html, /delivered/i);
  assert.match(renderNotices([]), /No notices/);
});

test('all renderers accept null/undefined/empty', () => {
  for (const v of [null, undefined, {}, [], '', 5]) {
    for (const fn of [renderGateSummary, renderMainStatus, renderQueue, renderNotices, renderPrDetail]) {
      assert.equal(typeof fn(v), 'string');
    }
    assert.equal(typeof renderPrTable(v, v), 'string');
  }
});
