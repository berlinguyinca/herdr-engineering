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
  const html = renderMainStatus({ repos: [{ repo: 'o/n', branch: 'main', head_sha: 'a'.repeat(40), state: 'red', last_green_sha: 'b'.repeat(40), failures: ['run 12 failure', '<img src=x>', 'f3', 'f4', 'f5', 'f6', 'f7'], range_total: 5, culprits: [11, 12] }] });
  assert.match(html, /o\/n/);
  assert.match(html, /red/i);
  assert.match(html, /b{12}/);
  assert.match(html, /#11/);
  assert.match(html, /#12/);
  assert.match(html, /<li>run 12 failure<\/li>/);
  assert.match(html, /<li>&lt;img src=x&gt;<\/li>/);
  assert.match(html, /<li>\+2 more<\/li>/);
  assert.doesNotMatch(html, /f6|f7/);
  assert.match(html, /5 commits in range/);
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
  ], gate: { last_tick_at: 1700000000, counts: { repos: 1, prs: 2, enqueued: 1, merged: 0, errors: 0, skipped_drafts: 0, main_errors: 0 }, last_error: EVIL } });
  noMarkup(html);
  assert.match(html, /1 claimed/);
  assert.match(html, /1 queued/);
  assert.match(html, /Depth 3/);
  assert.match(html, /&lt;img/);
  const sum = renderGateSummary({ gate: { last_tick_at: 1700000000, counts: { repos: 1, prs: 3, enqueued: 2, merged: 1, errors: 0 } } }, [{ state: 'escalated' }, { state: 'escalated' }, { state: 'approved' }, { state: 'weird' }]);
  assert.match(sum, /<strong>2<\/strong><span>Escalated/);
  assert.match(sum, /<strong>1<\/strong><span>Approved/);
  assert.match(sum, /<strong>1<\/strong><span>Unknown state/);
  assert.match(sum, /Last tick: 1 repos checked, 3 PRs seen, 2 reviews enqueued, 1 merged, 0 errors/);
  assert.doesNotMatch(sum, /aria-label="[^"]*state[^"]*"[^>]*><li[^>]*><strong>3/);
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
const QUEUE = { depth: 1, jobs: [{ job_id: 'j', key: 'github.com/o/n#2', sha: 'b'.repeat(40), state: 'queued' }], gate: { last_tick_at: 1700000000, counts: { repos: 1, prs: 2, enqueued: 1, merged: 0, errors: 0, skipped_drafts: 0, main_errors: 0 }, last_error: null } };
const NOTICES = [{ key: 'github.com/o/n#1', sha: 'a'.repeat(40), kind: 'escalated', channel: 'user', ok: true, at: 1700000000 }];
const MAIN = { repos: [{ repo: 'o/n', branch: 'main', head_sha: 'c'.repeat(40), state: 'green', last_green_sha: 'c'.repeat(40), failures: [], range_total: 0, culprits: [] }] };
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

test('a hung read times out on its own and only that panel warns', async () => {
  const sigs = [];
  const h = harness({ handler: (path, opts) => (routeOf(path) === 'notices'
    ? new Promise((_, rej) => opts.signal.addEventListener('abort', () => rej(Object.assign(new Error('signal timed out'), { name: 'TimeoutError' }))))
    : Promise.resolve(DATA[routeOf(path)])) });
  const view = mountReviewGate(h.container, h.client, { timers: h.timers, timeout: () => { const c = new AbortController(); sigs.push(c); return c.signal; } });
  await flush();
  assert.equal(sigs.length, 4);
  assert.doesNotMatch(h.container.innerHTML, /github\.com\/o\/n#1/);   // not settled yet: Promise.allSettled waits
  sigs.forEach((c) => c.abort());
  await flush();
  assert.match(h.container.innerHTML, /github\.com\/o\/n#1/);
  assert.match(h.container.innerHTML, /Could not load: request timed out/);
  view.dispose();
});

test('a successful resume applies the returned snapshot and clears the error', async () => {
  let mode = 'fail';
  const h = harness({ handler: (path, opts) => {
    if (opts.method === 'POST') return mode === 'fail' ? Promise.reject(new ApiError(503, { error: 'persist failed' }, 'x')) : Promise.resolve({ ...PRS[0], state: 'ci_pending', reason: null });
    if (mode === 'ok' && routeOf(path) === 'prs') return new Promise(() => {});   // only the POST snapshot can explain the change
    return Promise.resolve(DATA[routeOf(path)]);
  } });
  window.confirm = () => true;
  mountReviewGate(h.container, h.client, { timers: h.timers });
  await flush();
  h.click('rgSelect', 'github.com/o/n#1');
  h.click('rgResume', 'github.com/o/n#1');
  await flush();
  assert.match(h.container.innerHTML, /persist failed/);
  mode = 'ok';
  h.click('rgResume', 'github.com/o/n#1');
  await flush();
  assert.doesNotMatch(h.container.innerHTML, /persist failed/);
  assert.doesNotMatch(h.container.innerHTML, /data-rg-resume/);
  assert.match(h.container.innerHTML, /Waiting for CI/);
});

test('an in-flight poll started before the resume cannot bring the button back', async () => {
  let releaseRead, first = true, posted = false;
  const h = harness({ handler: (path, opts) => {
    if (opts.method === 'POST') { posted = true; return Promise.resolve({ ...PRS[0], state: 'ci_pending' }); }
    if (routeOf(path) === 'prs' && posted && !first) return new Promise(() => {});   // follow-up poll stays pending
    if (routeOf(path) === 'prs' && first) { first = false; return new Promise((r) => { releaseRead = () => r(PRS); }); }
    return Promise.resolve(DATA[routeOf(path)]);
  } });
  window.confirm = () => true;
  mountReviewGate(h.container, h.client, { timers: h.timers });
  await flush();                     // first poll hangs on prs
  releaseRead(); await flush();      // now data is escalated
  h.click('rgSelect', 'github.com/o/n#1');
  first = true;
  h.timers.fn(); await flush();      // poll 2 hangs on prs (stale escalated copy pending)
  h.click('rgResume', 'github.com/o/n#1');
  await flush();
  releaseRead(); await flush();      // stale escalated response arrives after the resume
  assert.doesNotMatch(h.container.innerHTML, /data-rg-resume/);
});

test('a poll showing the PR no longer escalated clears its old refusal', async () => {
  let state = 'escalated';
  const h = harness({ handler: (path, opts) => (opts.method === 'POST' ? Promise.reject(new ApiError(409, { error: 'pull request is not escalated' }, 'x'))
    : Promise.resolve(routeOf(path) === 'prs' ? [{ ...PRS[0], state }] : DATA[routeOf(path)])) });
  window.confirm = () => true;
  mountReviewGate(h.container, h.client, { timers: h.timers });
  await flush();
  h.click('rgSelect', 'github.com/o/n#1');
  h.click('rgResume', 'github.com/o/n#1');
  await flush();
  assert.match(h.container.innerHTML, /Resume refused: pull request is not escalated/);
  state = 'approved';
  h.timers.fn(); await flush();
  assert.doesNotMatch(h.container.innerHTML, /Resume refused/);
  state = 'escalated';
  h.timers.fn(); await flush();
  assert.doesNotMatch(h.container.innerHTML, /Resume refused/);
});

test('an unchanged poll does not rewrite the DOM; a changed one restores focus', async () => {
  const h = harness();
  let writes = 0, html = '';
  Object.defineProperty(h.container, 'innerHTML', { get: () => html, set: (v) => { writes++; html = v; } });
  const focused = [];
  const btn = { getAttribute: () => 'github.com/o/n#1', focus: () => focused.push('resume') };
  h.container.contains = () => true;
  h.container.querySelectorAll = (sel) => (sel === '[data-rg-resume]' ? [btn] : []);
  globalThis.document = { activeElement: { dataset: { rgResume: 'github.com/o/n#1' } } };
  try {
    mountReviewGate(h.container, h.client, { timers: h.timers });
    await flush();
    h.click('rgSelect', 'github.com/o/n#1');
    const after = writes;
    h.timers.fn(); await flush();
    assert.equal(writes, after);          // identical data: no write
    const before = focused.length;
    h.click('rgSelect', 'github.com/o/n#2');
    assert.ok(writes > after);
    h.click('rgSelect', 'github.com/o/n#1');
    assert.ok(focused.length > before);   // changed draw restored focus
  } finally { delete globalThis.document; }
});
