// The Reviews page: needs-decision first, then the pass/fail trend, then the full list. The test server keeps no durable
// reviews, so /api/v1/reviews is stubbed with a realistic mix; timers are real.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const REPO = path.resolve(import.meta.dirname, '..', '..');
const PY = process.env.PYTHON
  || (fs.existsSync(path.join(REPO, '.venv', 'bin', 'python')) ? path.join(REPO, '.venv', 'bin', 'python') : 'python3');
const SHOTS = process.env.REVIEWS_SHOTS || '';

async function startServer() {
  const proc = spawn(PY, [path.join(REPO, 'tests', 'playwright', 'server.py')], { env: { ...process.env, BURST: '0' }, stdio: ['ignore', 'pipe', 'pipe'] });
  const port = await new Promise((resolve, reject) => {
    let buf = '';
    const timer = setTimeout(() => reject(new Error('server start timeout')), 20000);
    proc.stdout.on('data', (d) => { buf += d.toString(); const m = buf.match(/PORT=(\d+)/); if (m) { clearTimeout(timer); resolve(Number(m[1])); } });
    proc.on('exit', (c) => reject(new Error(`server exited early (${c})`)));
  });
  return { proc, port };
}

async function withApp(fn, context = {}) {
  const srv = await startServer();
  let browser;
  try {
    const args = ['--no-sandbox'];
    browser = await chromium.launch(process.env.PLAYWRIGHT_CHROMIUM ? { headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM, args } : { headless: true, args });
    const page = await (await browser.newContext(context)).newPage();
    return await fn({ page, base: `http://127.0.0.1:${srv.port}` });
  } finally {
    if (browser) { try { await browser.close(); } catch { /* noop */ } }
    try { srv.proc.kill(); } catch { /* noop */ }
  }
}

const H = 3600000, D = 24 * H;
const iso = ms => new Date(Date.now() - ms).toISOString();
function mix() {
  return [
    { review_id: 'p-wait', title: 'Improve parser', kind: 'plan', status: 'awaiting_review', repository: 'lab/api', created_at: iso(9 * H), updated_at: iso(9 * H), review_url: 'https://review.example/one', session_id: 'agent-1', pull_request_number: 3 },
    { review_id: 'p-quiet', title: 'Quiet launcher', kind: 'plan', status: 'awaiting_review', stale: true, repository: 'lab/api', created_at: iso(5 * H), updated_at: iso(5 * H), review_url: 'https://review.example/two' },
    { review_id: 'c-open', title: 'Revise decoder', kind: 'code', status: 'changes_requested', repository: 'lab/web', pull_request_number: 12, created_at: iso(3 * D), completed_at: iso(2 * D), updated_at: iso(2 * D) },
    { review_id: 'c-done', title: 'Tighten validation', kind: 'code', status: 'changes_requested', repository: 'lab/web', pull_request_number: 9, created_at: iso(5 * D), completed_at: iso(4 * D), updated_at: iso(4 * D) },
    { review_id: 'a-1', title: 'Validation approved', kind: 'code', status: 'approved', repository: 'lab/web', pull_request_number: 9, created_at: iso(2 * D), completed_at: iso(D), updated_at: iso(D) },
    { review_id: 'a-2', title: 'Approved parser', kind: 'plan', status: 'approved', repository: 'lab/api', created_at: iso(6 * H), completed_at: iso(3 * H), updated_at: iso(3 * H) },
    { review_id: 'f-1', title: 'Crashed review', kind: 'code', status: 'failed', repository: 'lab/api', created_at: iso(8 * H), completed_at: iso(7 * H), updated_at: iso(7 * H) },
    { review_id: 'a-20', title: 'Older approval', kind: 'plan', status: 'approved', repository: 'lab/api', created_at: iso(21 * D), completed_at: iso(20 * D), updated_at: iso(20 * D) },
    { review_id: 'a-60', title: 'Much older approval', kind: 'plan', status: 'approved', repository: 'lab/api', created_at: iso(61 * D), completed_at: iso(60 * D), updated_at: iso(60 * D) },
    { review_id: 'u-1', title: 'Mystery review', kind: 'code', status: 'done', repository: 'lab/api', created_at: iso(6 * D), completed_at: iso(5 * D), updated_at: iso(5 * D) },
  ];
}
const OPEN = new Set(['starting', 'awaiting_review']);
async function stub(page, reviews, { hasMore = false, total = reviews.length, waiting = reviews.filter((r) => OPEN.has(r.status)) } = {}) {
  await page.route('**/api/v1/reviews', (r) => r.fulfill({ json: { available: true, reviews, total, has_more: hasMore, checked_at: new Date().toISOString() } }));
  await page.route('**/api/v1/reviews?needs_decision=1', (r) => r.fulfill({ json: { available: true, needs_decision: true, reviews: waiting, total: waiting.length, has_more: false, checked_at: new Date().toISOString() } }));
}
const rows = (page, sel) => page.locator(`${sel} [data-rv-item]`).evaluateAll((n) => n.map((e) => e.dataset.rvItem));
const noOverflow = (page) => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);

test('needs-decision section lists waiting reviews first with reasons and links, and the list below is unchanged', async () => {
  await withApp(async ({ page, base }) => {
    const errors = []; page.on('pageerror', (e) => errors.push(e.message));
    await stub(page, mix());
    await page.goto(`${base}/#/reviews`);
    await page.locator('[data-rv-decisions] [data-rv-item]').first().waitFor();
    // order: awaiting, stale, then the unanswered change request; the answered one and finished ones are absent
    assert.deepEqual(await rows(page, '[data-rv-decisions]'), ['p-wait', 'p-quiet', 'c-open']);
    assert.equal(await page.locator('[data-rv-count]').textContent(), '3');
    const first = page.locator('[data-rv-item="p-wait"]');
    assert.match(await first.textContent(), /Improve parser/);
    assert.match(await first.textContent(), /lab\/api/);
    assert.match(await first.textContent(), /Waiting for your decision in Plannotator\. Waiting 9 h/);
    assert.equal(await first.getByRole('link', { name: 'Open in Plannotator' }).getAttribute('href'), 'https://review.example/one');
    assert.equal(await first.getByRole('link', { name: 'PR #3 trail' }).getAttribute('href'), '#/pr/lab/api/3');
    assert.ok(await first.getByRole('link', { name: 'Agent', exact: true }).count());
    assert.match(await page.locator('[data-rv-item="p-quiet"]').textContent(), /launcher stopped reporting/);
    assert.match(await page.locator('[data-rv-item="c-open"]').textContent(), /no later completed review of the same pull request or agent exists/);
    // sections are in the stated order on the page
    const order = await page.evaluate(() => ['[data-rv-decisions]', '[data-rv-trend]', '.ig-tabs', '#ig-results'].map((s) => document.querySelector(s).getBoundingClientRect().top));
    assert.deepEqual(order, [...order].sort((a, b) => a - b));
    // the full list keeps its tabs, counts and search exactly as before
    assert.deepEqual(await page.locator('.ig-tabs [role=tab]').evaluateAll((n) => n.map((e) => e.dataset.tab)), ['current', 'changes', 'failed', 'finished']);
    await page.locator('#ig-results').getByText('Improve parser', { exact: true }).waitFor();
    await page.getByRole('tab', { name: /^Changes requested/ }).click();
    await page.locator('#ig-results').getByText('Revise decoder', { exact: true }).waitFor();
    await page.getByRole('tab', { name: /^Failed/ }).click();
    await page.locator('#ig-results').getByText('Crashed review', { exact: true }).waitFor();
    await page.getByRole('tab', { name: /^Finished/ }).click();
    await page.locator('#ig-results').getByText('Approved parser', { exact: true }).waitFor();
    await page.getByRole('searchbox', { name: 'Search reviews' }).fill('missing project');
    assert.match(await page.locator('#ig-results').textContent(), /No reviews match this view/);
    assert.deepEqual(errors, []);
  });
});

test('trend shows approved, changes and failed per day, keeps unknown apart, and states its coverage', async () => {
  await withApp(async ({ page, base }) => {
    await stub(page, mix());
    await page.goto(`${base}/#/reviews`);
    await page.locator('[data-rv-trend] svg').waitFor();
    const note = await page.locator('[data-rv-covers]').textContent();
    // decided in the last 30 days: c-open, c-done, a-1, a-2, a-20, f-1 plotted; a-60 is outside; u-1 has no recorded outcome; two are not yet decided
    assert.match(note, /6 of 10 loaded reviews are plotted/);
    assert.match(note, /1 with no recorded outcome/);
    assert.match(note, /2 not yet decided/);
    assert.match(note, /never counted as approved or failed/);
    await page.locator('.rv-data summary').click();
    const table = await page.locator('.rv-data tbody tr').evaluateAll((trs) => trs.map((tr) => [...tr.children].map((c) => c.textContent)));
    const sum = (i) => table.reduce((n, r) => n + Number(r[i]), 0);
    assert.deepEqual([sum(1), sum(2), sum(3)], [3, 2, 1]);   // approved, changes requested, failed
    assert.equal(await page.locator('[data-rv-trend] svg').getAttribute('aria-labelledby'), 'rv-outcomes-title rv-outcomes-desc');
    // period: 7 days drops the review decided 20 days ago, which also changes the covered count
    await page.locator('[data-rv-period]').selectOption('7');
    await page.waitForFunction(() => /5 of 10 loaded reviews are plotted/.test(document.querySelector('[data-rv-covers]').textContent));
    await page.locator('[data-rv-period]').selectOption('90');
    await page.waitForFunction(() => /7 of 10 loaded reviews are plotted/.test(document.querySelector('[data-rv-covers]').textContent));
    assert.equal(await page.locator('[data-rv-period]').inputValue(), '90');
  });
});

test('empty state says nothing is waiting, and the trend says when no outcome is recorded', async () => {
  await withApp(async ({ page, base }) => {
    await stub(page, [{ review_id: 'x', title: 'Old', kind: 'plan', status: 'done', repository: 'lab/api' }]);
    await page.goto(`${base}/#/reviews`);
    await page.locator('[data-rv-clear]').waitFor();
    assert.match(await page.locator('[data-rv-decisions]').textContent(), /Nothing is waiting for a decision/);
    assert.equal(await page.locator('[data-rv-count]').textContent(), '0');
    assert.match(await page.locator('[data-rv-trend]').textContent(), /No review with a recorded outcome/);
    assert.match(await page.locator('[data-rv-covers]').textContent(), /0 of 1 loaded reviews are plotted; not plotted: 1 with no recorded outcome/);
  });
});

test('loading shows placeholders, a failed load keeps the retry notice, and neither shows a false "nothing is waiting"', async () => {
  await withApp(async ({ page, base }) => {
    let release;
    const gate = new Promise((r) => { release = r; });
    await page.route('**/api/v1/reviews', async (r) => { await gate; return r.fulfill({ status: 503, json: { error: 'down' } }); });
    await page.goto(`${base}/#/reviews`);
    await page.locator('[data-rv-decisions] [data-skeleton]').waitFor();
    assert.equal(await page.locator('[data-rv-clear]').count(), 0);
    release();
    await page.waitForFunction(() => document.querySelector('[data-ig-error]')?.textContent.trim());
    assert.equal(await page.locator('[data-rv-clear]').count(), 0);
    await page.locator('[data-rv-decisions] [data-rv-unavailable]').waitFor();
    assert.equal(await page.locator('[data-rv-decisions] [data-skeleton], [data-rv-trend] [data-skeleton]').count(), 0);
    assert.match(await page.locator('#ig-results').textContent(), /retrying automatically|could not be loaded/i);
  });
});

test('390px: no horizontal overflow and 44px controls; dark theme uses defined tokens', async () => {
  await withApp(async ({ page, base }) => {
    await page.emulateMedia({ colorScheme: 'dark' });
    await page.setViewportSize({ width: 390, height: 844 });
    await stub(page, mix());
    await page.goto(`${base}/#/reviews`);
    await page.locator('[data-rv-trend] svg').waitFor();
    assert.equal(await noOverflow(page), true);
    const small = await page.evaluate(() => [...document.querySelectorAll('[data-rv-decisions] a, [data-rv-period], .rv-data summary')]
      .filter((e) => e.getBoundingClientRect().height && e.getBoundingClientRect().height < 44).map((e) => e.textContent.trim() || e.tagName));
    assert.deepEqual(small, []);
    const colours = await page.evaluate(() => {
      const s = getComputedStyle(document.querySelector('.rv-section'));
      return { bg: s.backgroundColor, border: s.borderTopColor, body: getComputedStyle(document.body).backgroundColor };
    });
    assert.notEqual(colours.bg, 'rgba(0, 0, 0, 0)');
    assert.notEqual(colours.bg, 'rgb(255, 255, 255)');
    if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await page.screenshot({ path: path.join(SHOTS, 'reviews-phone-dark.png'), fullPage: true }); }
  });
});

test('desktop light screenshot and no overflow with a long title', async () => {
  await withApp(async ({ page, base }) => {
    await page.emulateMedia({ colorScheme: 'light' });
    await page.setViewportSize({ width: 1280, height: 900 });
    const reviews = mix();
    reviews[0].title = 'A'.repeat(200);
    await stub(page, reviews);
    await page.goto(`${base}/#/reviews`);
    await page.locator('[data-rv-trend] svg').waitFor();
    assert.equal(await noOverflow(page), true);
    if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await page.screenshot({ path: path.join(SHOTS, 'reviews-desktop-light.png'), fullPage: true }); }
  });
});

test('a truncated review list never hides a long-waiting review and never says nothing is waiting', async () => {
  await withApp(async ({ page, base }) => {
    const recent = [{ review_id: 'new-ok', title: 'Recent approval', kind: 'plan', status: 'approved', repository: 'lab/api', created_at: iso(2 * H), completed_at: iso(H), updated_at: iso(H) }];
    const longWait = { review_id: 'long-wait', title: 'Waiting for weeks', kind: 'plan', status: 'awaiting_review', repository: 'lab/api', created_at: iso(30 * D), updated_at: iso(30 * D), review_url: 'https://review.example/long' };
    await stub(page, recent, { hasMore: true, total: 812, waiting: [longWait] });
    await page.goto(`${base}/#/reviews`);
    await page.locator('[data-rv-item="long-wait"]').waitFor();
    assert.deepEqual(await rows(page, '[data-rv-decisions]'), ['long-wait']);
    assert.equal(await page.locator('[data-rv-clear]').count(), 0);
    assert.match(await page.locator('[data-rv-caveat]').textContent(), /1 of 812 reviews are loaded; older reviews may have changes requested that nobody answered/);
    assert.match(await page.locator('[data-rv-covers]').textContent(), /1 of 812 reviews are loaded; older outcomes are missing/);
    assert.doesNotMatch(await page.locator('[data-rv-covers]').textContent(), /covers every review/);
  });
});

test('with a truncated list and no waiting review the empty state is a caveat, not "nothing is waiting"', async () => {
  await withApp(async ({ page, base }) => {
    await stub(page, [{ review_id: 'a', title: 'Approved', kind: 'plan', status: 'approved', repository: 'lab/api', created_at: iso(2 * H), completed_at: iso(H), updated_at: iso(H) }], { hasMore: true, total: 812, waiting: [] });
    await page.goto(`${base}/#/reviews`);
    await page.locator('[data-rv-partial]').waitFor();
    assert.equal(await page.locator('[data-rv-clear]').count(), 0);
    assert.doesNotMatch(await page.locator('[data-rv-decisions]').textContent(), /Nothing is waiting/);
    assert.match(await page.locator('[data-rv-decisions]').textContent(), /1 of 812 reviews are loaded/);
  });
});

test('the list disclosure owns the review detail: the decision section has none, and one open review stays one across refreshes', async () => {
  await withApp(async ({ page, base }) => {
    const reviews = mix();
    await stub(page, reviews);
    await page.route('**/api/v1/reviews/p-wait', (r) => r.fulfill({ json: { ...reviews[0], plan: '## Plan\nDetail text' } }));
    await page.goto(`${base}/#/reviews`);
    await page.locator('[data-rv-item="p-wait"]').waitFor();
    assert.equal(await page.locator('[data-rv-decisions] [data-review-details]').count(), 0);
    assert.ok(await page.locator('[data-rv-item="p-wait"]').getByRole('link', { name: 'Open in Plannotator' }).count());
    const summary = page.locator('#ig-results [data-review-details="p-wait"] summary');
    await summary.click();
    await page.waitForFunction(() => document.querySelector('#ig-results .ig-log')?.textContent.includes('Detail text'));
    await summary.focus();
    await page.waitForTimeout(6000);   // at least one real refresh and redraw of both sections
    assert.equal(await page.locator('[data-review-details][open]').count(), 1);
    assert.equal(await page.locator('#ig-results [data-review-details="p-wait"]').evaluate((e) => e.open), true);
    assert.equal(await page.evaluate(() => document.activeElement?.tagName), 'SUMMARY');
    assert.equal(await page.locator('[data-rv-decisions] [data-review-details]').count(), 0);
  });
});

test('the needs-decision count is a status region and the sections use only defined colours', async () => {
  await withApp(async ({ page, base }) => {
    await stub(page, mix());
    await page.goto(`${base}/#/reviews`);
    await page.locator('[data-rv-trend] svg').waitFor();
    assert.equal(await page.locator('[data-rv-count]').getAttribute('role'), 'status');
    const css = await page.evaluate(() => [...document.querySelectorAll('style')].map((s) => s.textContent).join('').match(/\.rv-[^{]+\{[^}]*\}/g).join(''));
    const vars = [...new Set([...css.matchAll(/var\((--[a-z-]+)\)/g)].map((m) => m[1]).filter((n) => n !== '--chart-color'))];   // --chart-color is set per legend entry
    const undefinedVars = await page.evaluate((names) => names.filter((n) => !getComputedStyle(document.documentElement).getPropertyValue(n).trim()), vars);
    assert.deepEqual(undefinedVars, []);
  });
});
