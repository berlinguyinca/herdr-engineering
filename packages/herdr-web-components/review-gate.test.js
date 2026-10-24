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

// ---- mountReviewGate -------------------------------------------------------
import { mountReviewGate } from '../review-gate-view.js';
import { ApiError } from '../herdr-web-client/client.js';

const PRS = [
  { key: 'github.com/o/n#1', head_sha: 'a'.repeat(40), state: 'escalated', review_rounds: 3, ci_fix_rounds: 0, reason: 'repeated finding', last_findings: [{ path: 'x.py', line: 1, severity: 'high', message: 'bad' }] },
  { key: 'github.com/o/n#2', head_sha: 'b'.repeat(40), state: 'approved', review_rounds: 1, ci_fix_rounds: 0, last_findings: [] },
];
const QUEUE = { depth: 1, jobs: [{ job_id: 'j', key: 'github.com/o/n#2', sha: 'b'.repeat(40), state: 'queued' }], gate: { last_tick_at: 1700000000, counts: { escalated: 1, approved: 1 }, last_error: null } };
const NOTICES = [{ key: 'github.com/o/n#1', sha: 'a'.repeat(40), kind: 'escalated', channel: 'user', ok: true, at: 1700000000 }];
const MAIN = { repos: [{ repo: 'o/n', branch: 'main', head_sha: 'c'.repeat(40), state: 'green', last_green_sha: 'c'.repeat(40), failures: 0, range_total: 0, culprits: [] }] };
const DATA = { prs: PRS, queue: QUEUE, notices: NOTICES, main: MAIN };
const routeOf = (path) => path.replace('/api/v1/review-gate/', '').split('/')[0];

function harness({ handler } = {}) {
  const calls = [];
  const client = { request: (path, opts = {}) => {
    calls.push({ path, ...opts });
    return handler ? handler(path, opts, calls) : Promise.resolve(DATA[routeOf(path)]);
  } };
  const handlers = new Map();
  const container = { innerHTML: '', addEventListener: (t, f) => handlers.set(t, f), removeEventListener: (t) => handlers.delete(t) };
  const timers = { fn: null, cleared: false, setInterval(fn) { this.fn = fn; return 7; }, clearInterval(id) { if (id === 7) this.cleared = true; } };
  const click = (name, value) => handlers.get('click')({ target: { closest: (sel) => (sel.includes(name.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase())) ? { dataset: { [name]: value } } : null) }, preventDefault() {} });
  return { calls, client, container, timers, handlers, click };
}
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise((r) => setImmediate(r)); };
const reads = (calls) => calls.filter((c) => !c.method || c.method === 'GET');
globalThis.window = globalThis.window || {};

test('mount polls the four routes and renders', async () => {
  const h = harness();
  const view = mountReviewGate(h.container, h.client, { timers: h.timers });
  await flush();
  assert.deepEqual(reads(h.calls).map((c) => c.path).sort(), ['/api/v1/review-gate/main', '/api/v1/review-gate/notices', '/api/v1/review-gate/prs', '/api/v1/review-gate/queue']);
  assert.ok(reads(h.calls).every((c) => c.signal));
  assert.match(h.container.innerHTML, /github\.com\/o\/n#1/);
  assert.match(h.container.innerHTML, /o\/n/);
  assert.match(h.container.innerHTML, /delivered/);
  view.dispose();
});

test('mount keeps last good data and warns when a read fails', async () => {
  let fail = false;
  const h = harness({ handler: (path) => (fail && routeOf(path) === 'queue' ? Promise.reject(new ApiError(500, null, '<b>boom</b>')) : Promise.resolve(DATA[routeOf(path)])) });
  const view = mountReviewGate(h.container, h.client, { timers: h.timers });
  await flush();
  assert.doesNotMatch(h.container.innerHTML, /Showing data from/);
  fail = true;
  h.timers.fn(); await flush();
  assert.match(h.container.innerHTML, /role="status"[^>]*>[^<]*Showing data from/);
  assert.match(h.container.innerHTML, /&lt;b&gt;boom/);
  assert.match(h.container.innerHTML, /github\.com\/o\/n#2/);   // page not blanked
  assert.match(h.container.innerHTML, /github\.com\/o\/n#1/);
  view.dispose();
});

test('a read that never succeeded says so instead of showing an empty table as truth', async () => {
  const h = harness({ handler: (path) => (routeOf(path) === 'main' ? Promise.reject(new ApiError(503, null, 'down')) : Promise.resolve(DATA[routeOf(path)])) });
  mountReviewGate(h.container, h.client, { timers: h.timers });
  await flush();
  assert.match(h.container.innerHTML, /Could not load[^<]*down/);
});

test('404 from a route shows the disabled note and stops polling', async () => {
  const h = harness({ handler: () => Promise.reject(new ApiError(404, null, 'not found')) });
  mountReviewGate(h.container, h.client, { timers: h.timers });
  await flush();
  assert.match(h.container.innerHTML, /Review gate is not enabled on this control plane/);
  assert.equal(h.timers.cleared, true);
  const n = h.calls.length;
  h.timers.fn(); await flush();
  assert.equal(h.calls.length, n);
});

test('dispose stops polling and aborts in-flight reads', async () => {
  const h = harness({ handler: (path, opts) => new Promise((_, reject) => opts.signal.addEventListener('abort', () => reject(new Error('aborted')))) });
  const view = mountReviewGate(h.container, h.client, { timers: h.timers });
  const signals = h.calls.map((c) => c.signal);
  assert.equal(signals.length, 4);
  view.dispose();
  assert.ok(signals.every((s) => s.aborted));
  assert.equal(h.timers.cleared, true);
  assert.equal(h.handlers.size, 0);
  const before = h.container.innerHTML;
  await flush();
  assert.equal(h.container.innerHTML, before);
  view.dispose();   // idempotent
});

test('no overlapping polls', async () => {
  const h = harness({ handler: () => new Promise(() => {}) });
  mountReviewGate(h.container, h.client, { timers: h.timers });
  h.timers.fn(); h.timers.fn(); await flush();
  assert.equal(h.calls.length, 4);
});

test('resume posts to the route after confirm and refreshes', async () => {
  const h = harness({ handler: (path, opts) => (opts.method === 'POST' ? Promise.resolve({ ok: true }) : Promise.resolve(DATA[routeOf(path)])) });
  const asked = [];
  window.confirm = (m) => { asked.push(m); return true; };
  const view = mountReviewGate(h.container, h.client, { timers: h.timers });
  await flush();
  h.click('rgSelect', 'github.com/o/n#1');
  assert.match(h.container.innerHTML, /data-rg-resume="github.com\/o\/n#1"/);
  const readsBefore = reads(h.calls).length;
  h.click('rgResume', 'github.com/o/n#1');
  await flush();
  assert.equal(asked.length, 1);
  const post = h.calls.find((c) => c.method === 'POST');
  assert.equal(post.path, '/api/v1/review-gate/prs/github.com/o/n/1/resume');
  assert.deepEqual(post.body, {});
  assert.equal(reads(h.calls).length, readsBefore + 4);
  view.dispose();
});

test('resume does nothing when the operator declines', async () => {
  const h = harness();
  window.confirm = () => false;
  mountReviewGate(h.container, h.client, { timers: h.timers });
  await flush();
  h.click('rgSelect', 'github.com/o/n#1');
  h.click('rgResume', 'github.com/o/n#1');
  await flush();
  assert.equal(h.calls.some((c) => c.method === 'POST'), false);
});

test('resume shows the refusal on 403 and leaves state unchanged', async () => {
  const h = harness({ handler: (path, opts) => (opts.method === 'POST' ? Promise.reject(new ApiError(403, { error: '<i>nope</i>' }, 'forbidden')) : Promise.resolve(DATA[routeOf(path)])) });
  window.confirm = () => true;
  mountReviewGate(h.container, h.client, { timers: h.timers });
  await flush();
  h.click('rgSelect', 'github.com/o/n#1');
  h.click('rgResume', 'github.com/o/n#1');
  await flush();
  assert.match(h.container.innerHTML, /operator/i);
  assert.match(h.container.innerHTML, /data-rg-resume/);        // still escalated, button back
  assert.doesNotMatch(h.container.innerHTML, /data-rg-resume[^>]*disabled/);
  assert.match(h.container.innerHTML, /Escalated/);

  const h2 = harness({ handler: (path, opts) => (opts.method === 'POST' ? Promise.reject(new ApiError(409, { error: '<i>not escalated</i>' }, 'x')) : Promise.resolve(DATA[routeOf(path)])) });
  mountReviewGate(h2.container, h2.client, { timers: h2.timers });
  await flush();
  h2.click('rgSelect', 'github.com/o/n#1');
  h2.click('rgResume', 'github.com/o/n#1');
  await flush();
  assert.match(h2.container.innerHTML, /&lt;i&gt;not escalated/);
  assert.doesNotMatch(h2.container.innerHTML, /<i>/);
});

test('resume disables the button while in flight', async () => {
  let release;
  const h = harness({ handler: (path, opts) => (opts.method === 'POST' ? new Promise((r) => { release = r; }) : Promise.resolve(DATA[routeOf(path)])) });
  window.confirm = () => true;
  mountReviewGate(h.container, h.client, { timers: h.timers });
  await flush();
  h.click('rgSelect', 'github.com/o/n#1');
  h.click('rgResume', 'github.com/o/n#1');
  assert.match(h.container.innerHTML, /data-rg-resume[^>]*disabled/);
  h.click('rgResume', 'github.com/o/n#1');   // second click ignored
  assert.equal(h.calls.filter((c) => c.method === 'POST').length, 1);
  release({}); await flush();
});

test('resume not offered for non-escalated PRs', async () => {
  const h = harness();
  mountReviewGate(h.container, h.client, { timers: h.timers });
  await flush();
  h.click('rgSelect', 'github.com/o/n#2');
  assert.doesNotMatch(h.container.innerHTML, /data-rg-resume/);
  h.click('rgResume', 'github.com/o/n#2');   // forged click is refused too
  await flush();
  assert.equal(h.calls.some((c) => c.method === 'POST'), false);
});

test('selection survives a poll', async () => {
  const h = harness();
  mountReviewGate(h.container, h.client, { timers: h.timers });
  await flush();
  h.click('rgSelect', 'github.com/o/n#1');
  h.timers.fn(); await flush();
  assert.match(h.container.innerHTML, /data-rg-resume="github.com\/o\/n#1"/);
  assert.match(h.container.innerHTML, /aria-pressed="true"/);
});
