import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {chromium} from 'playwright';

const root = path.resolve(import.meta.dirname, '../..');
const python = process.env.PYTHON || (fs.existsSync(path.join(root, '.venv/bin/python')) ? path.join(root, '.venv/bin/python') : 'python3');
// Review screenshots go to a scratch dir, never the tracked docs tree (two curated ones live in evidence/).
const shots = fs.mkdtempSync(path.join(os.tmpdir(), 'herdr-architecture-shots-'));

async function withApp(env, fn, ctx = {}) {
  const server = spawn(python, [path.join(root, 'tests/playwright/architecture-server.py')], {env: {...process.env, ...env}, stdio: ['ignore', 'pipe', 'pipe']});
  let browser;
  try {
    const port = await new Promise((resolve, reject) => {
      let out = ''; const t = setTimeout(() => reject(new Error('Fixture startup timed out')), 60000);
      server.stdout.on('data', d => { out += d; const m = out.match(/PORT=(\d+)/); if (m) { clearTimeout(t); resolve(m[1]); } });
      server.stderr.on('data', d => { out += d; });
      server.once('exit', c => { clearTimeout(t); reject(new Error(`Fixture exited ${c}: ${out.slice(-500)}`)); });
    });
    const options = {headless: true, args: ['--no-sandbox']};
    if (process.env.PLAYWRIGHT_CHROMIUM) options.executablePath = process.env.PLAYWRIGHT_CHROMIUM;
    try { browser = await chromium.launch(options); } catch (e) {
      const fb = '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
      if (!fs.existsSync(fb)) throw e; browser = await chromium.launch({...options, executablePath: fb});
    }
    const page = await browser.newPage(ctx);
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    await fn(page, `http://127.0.0.1:${port}`);
    assert.deepEqual(errors, [], 'no uncaught page errors');
  } finally { await browser?.close(); server.kill(); }
}
const shot = (page, name) => page.screenshot({path: path.join(shots, name), fullPage: true});

test('disabled control plane shows an explanatory empty state, not an error', () => withApp({ARCH_MODE: 'disabled'}, async (page, base) => {
  await page.goto(`${base}/#/architecture`);
  await page.getByText('not enabled on this control plane').waitFor();
}));

test('empty state tells the operator how to produce a snapshot', () => withApp({ARCH_MODE: 'empty'}, async (page, base) => {
  await page.goto(`${base}/#/architecture`);
  await page.getByText('No architecture snapshots yet').waitFor();
  assert.match(await page.locator('herdr-empty').first().getAttribute('message'), /architecture analyze/);
}));

test('real analyzer output: notices, drilldown to symbol and source line, edge evidence', () => withApp({}, async (page, base) => {
  await page.goto(`${base}/#/architecture`);
  await page.locator('[data-testid="arch-graph"]').waitFor();
  // newest snapshot has a .ts file => partial, and is not stale
  await page.locator('[data-notice="partial"]').waitFor();
  assert.equal(await page.locator('[data-notice="stale"]').count(), 0);
  // the older snapshot is stale because a newer one exists
  await page.locator('[data-snap]').selectOption({index: 1});
  await page.locator('[data-notice="stale"]').waitFor();
  await page.locator('[data-snap]').selectOption({index: 0});
  await page.locator('[data-node="package:app"]').first().click();
  await page.locator('[data-node="module:app.core"]').waitFor();
  assert.ok(await page.locator('path.arch-edge').count() >= 2, 'aggregated import edges are drawn');
  await page.locator('[data-node="module:app.core"]').click();
  await page.locator('[data-node="class:app.core.Engine"]').waitFor();
  await page.locator('[data-open="function:app.core.run"]').click();
  await page.locator('.arch-detail').getByText('app/core.py:9').waitFor();       // exact line of `def run`
  // edge evidence: app.util is imported by app.core (static, with extractor and line)
  await page.locator('[data-crumb="package:app"]').click();
  await page.locator('[data-open="module:app.util"]').click();
  await page.locator('.arch-detail [data-edge]').first().waitFor();
  assert.match(await page.locator('.arch-detail [data-edge]').first().innerText(), /\.py:\d+ · static · python-ast-import/);
  await shot(page, 'architecture-light-wide.png');
}));

test('search is bounded, then results link to the right file:line', () => withApp({}, async (page, base) => {
  await page.goto(`${base}/#/architecture`);
  await page.locator('[data-q]').fill('helper');
  await page.locator('.arch-table button', {hasText: 'app.util.helper'}).waitFor();
  assert.match(await page.locator('.arch-table tbody tr').first().innerText(), /app\/util\.py:3/);
  await page.locator('[data-q]').fill('zzzz-nothing');
  await page.getByText('No matching symbols').waitFor();
}));

test('diff between snapshots lists added/removed/modified with the rename warning', () => withApp({}, async (page, base) => {
  await page.goto(`${base}/#/architecture`);
  await page.locator('[data-diff]').waitFor();
  const n = await page.locator('[data-diff] option').count();
  await page.locator('[data-diff]').selectOption({index: n - 1});   // oldest fixture snapshot
  const sum = page.locator('[data-testid="arch-diff-summary"]');
  await sum.waitFor();
  assert.match(await sum.innerText(), /Nodes \+\d+ .*edges \+\d+/);
  await page.locator('.chg-added', {hasText: 'function:app.util.added'}).waitFor();
  await page.locator('.chg-modified', {hasText: 'function:app.util.helper'}).waitFor();
  await page.getByText('rename detection is not implemented').waitFor();
}));

test('failed and running analyses are surfaced', () => withApp({ARCH_MODE: 'failed'}, async (page, base) => {
  await page.goto(`${base}/#/architecture`);
  await page.locator('[role="alert"]', {hasText: 'wall-clock limit'}).waitFor();
  await page.locator('.arch-notice.running', {hasText: '40%'}).waitFor();
}));

test('keyboard: graph nodes are focusable and Enter drills; table is a real table with headers', () => withApp({}, async (page, base) => {
  await page.goto(`${base}/#/architecture`);
  const node = page.locator('[data-node="package:app"]').first();
  await node.waitFor();
  assert.equal(await node.getAttribute('role'), 'button');
  assert.match(await node.getAttribute('aria-label'), /package app, \d+ children/);
  await node.focus(); await page.keyboard.press('Enter');
  await page.locator('[data-node="module:app.core"]').waitFor();
  assert.equal(await page.locator('table.arch-table th[scope="col"]').count(), 4);
  assert.ok(await page.locator('table.arch-table caption').innerText());
}));

// A live refresh can replace an element between locating it and reading its style; getComputedStyle of a
// detached node returns '' for every property. Retry until the value is a real colour (it never is a
// permanent state: the view always has these elements once rendered).
async function styleOf(page, selector, prop) {
  for (let i = 0; i < 20; i++) {
    const v = await page.locator(selector).first().evaluate((e, p) => getComputedStyle(e)[p], prop);
    if (/\d/.test(v)) return v;
    await page.waitForTimeout(100);
  }
  throw new Error(`${selector} never reported a computed ${prop}`);
}

for (const [name, ctx] of [['dark-wide', {colorScheme: 'dark', viewport: {width: 1440, height: 900}}],
  ['light-mobile', {colorScheme: 'light', viewport: {width: 390, height: 844}}],
  ['dark-mobile', {colorScheme: 'dark', viewport: {width: 390, height: 844}}],
  ['light-4k', {colorScheme: 'light', viewport: {width: 3840, height: 2160}}]]) {
  test(`layout ${name}: no page-level horizontal overflow, readable contrast surface`, () => withApp({}, async (page, base) => {
    await page.goto(`${base}/#/architecture`);
    await page.locator('[data-node="package:app"]').first().waitFor();
    await page.locator('[data-node="package:app"]').first().click();
    await page.locator('[data-node="module:app.core"]').waitFor();
    const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok(over <= 1, `horizontal overflow ${over}px`);
    const bg = await styleOf(page, '.arch', 'backgroundColor');
    if (ctx.colorScheme === 'dark') assert.equal(bg, 'rgb(22, 24, 29)'); else assert.equal(bg, 'rgb(255, 255, 255)');
    // headings must actually be readable on the view's own surface (WCAG AA 4.5:1)
    const lum = c => { const [r, g, b] = c.match(/\d+/g).slice(0, 3).map(Number).map(v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }); return .2126 * r + .7152 * g + .0722 * b; };
    const ratio = (x, y) => { const [hi, lo] = [lum(x), lum(y)].sort((m, n) => n - m); return (hi + .05) / (lo + .05); };
    for (const sel of ['.arch h1', '.arch .arch-meta', '.arch-table td', '.arch-activity h2', '.arch-activity h3', '.arch-activity li', '.arch-timeline h3', '.arch-timeline small', '.arch-request label', '.arch-note']) {
      const fg = await styleOf(page, sel, 'color');
      assert.ok(ratio(fg, bg) >= 4.5, `${sel} contrast ${ratio(fg, bg).toFixed(2)} on ${name}: ${fg} vs ${bg}`);
    }
    const nodeFill = await styleOf(page, '.arch-node rect', 'fill');
    const nodeText = await styleOf(page, '.arch-node .t1', 'fill');
    assert.ok(ratio(nodeText, nodeFill) >= 4.5, `node label contrast on ${name}: ${nodeText} vs ${nodeFill}`);
    await shot(page, `architecture-${name}.png`);
  }, ctx));
}

test('own repository graph renders (representative repo) and stays interactive', { timeout: 120000 }, () => withApp({ARCH_SELF: '1'}, async (page, base) => {
  await page.goto(`${base}/#/architecture`);
  await page.locator('[data-snap]').waitFor();
  const opts = await page.locator('[data-snap] option').allInnerTexts();
  const self = opts.findIndex(o => o.includes('berlinguyinca/herdr-engineering'));
  assert.ok(self >= 0, 'self snapshot listed');
  await page.locator('[data-snap]').selectOption({index: self});
  await page.locator('[data-node="package:herdr_engineering"]').first().click();
  await page.locator('[data-node="package:herdr_engineering.control_plane"]').waitFor();
  await page.locator('[data-node="package:herdr_engineering.control_plane"]').click();
  await page.locator('[data-node="module:herdr_engineering.control_plane.api"]').waitFor();
  await page.locator('[data-node="module:herdr_engineering.control_plane.api"]').click();
  await page.locator('.arch-detail [data-edge]').first().waitFor();
  await shot(page, 'architecture-herdr-self.png');
}));

test('related activity links only by exact commit; timeline has independent lanes', () => withApp({}, async (page, base) => {
  await page.goto(`${base}/#/architecture`);
  const act = page.locator('[data-testid="arch-activity"]');
  await act.waitFor();
  assert.match(await act.locator('[data-ci]').innerText(), /#5 · success/);
  assert.match(await act.locator('[data-pr]').innerText(), /#17|ci_pending/);
  assert.match(await act.locator('[data-session]').innerText(), /pi:s1/);
  assert.equal(await act.locator('[data-session]').count(), 1, 'the branch-only session is not linked');
  const tl = page.locator('[data-testid="arch-timeline"]');
  await tl.locator('[data-tl-snap]').first().waitFor();
  assert.ok(await tl.locator('[data-tl-snap]').count() >= 3);
  await tl.locator('[data-tl-agent]').first().waitFor();
  assert.match(await tl.innerText(), /Architecture snapshots[\s\S]*Agent spawns/);
  await shot(page, 'architecture-activity-timeline.png');
}));

test('request analysis reports queued, then deduplicated', () => withApp({}, async (page, base) => {
  await page.goto(`${base}/#/architecture`);
  await page.locator('[data-request]').waitFor();
  await page.locator('[data-request] button[type=submit]').click();
  const result = page.locator('[data-request-result]');
  await result.getByText(/^Queued \S+\./).waitFor();
  await page.locator('[data-request] button[type=submit]').click();
  await result.getByText(/^Already queued/).waitFor();
}));

test('cancel a queued analysis: confirm, in-flight disable, escaped refusal', () => withApp({}, async (page, base) => {
  await page.goto(`${base}/#/architecture`);
  await page.locator('[data-request]').waitFor();
  await page.locator('[data-request] button[type=submit]').click();
  const jobs = page.locator('[data-testid="arch-jobs"]');
  const btn = jobs.locator('[data-cancel]').first();
  await btn.waitFor();
  const id = await btn.getAttribute('data-cancel');
  assert.match(await jobs.innerText(), /requested/);
  // Declining the confirm sends nothing.
  let posts = 0;
  page.on('request', r => { if (r.method() === 'POST' && r.url().endsWith('/cancel')) posts++; });
  page.once('dialog', d => d.dismiss());
  await btn.click();
  await page.waitForTimeout(300);
  assert.equal(posts, 0);
  // A refusal (as a non-operator key would get) is shown as text, never markup, and the button comes back.
  let release;
  const held = new Promise(r => { release = r; });
  await page.route('**/api/v1/architecture/analyses/*/cancel', async route => {
    await held;
    await route.fulfill({status: 403, contentType: 'application/json', body: JSON.stringify({error: '<img src=x onerror="window.__pwned=1">denied'})});
  });
  page.once('dialog', d => d.accept());
  await btn.click();
  await jobs.locator(`[data-cancel="${id}"][disabled]`).waitFor();             // in flight: disabled
  release();
  const err = jobs.locator('[data-cancel-error]');
  await err.waitFor();
  assert.equal(await err.innerText(), 'Only an operator key can cancel analyses: <img src=x onerror="window.__pwned=1">denied');
  assert.equal(await jobs.locator('img').count(), 0);
  assert.equal(await page.evaluate(() => window.__pwned), undefined);
  assert.equal(await btn.isDisabled(), false);
  // The real (open-mode, operator) server cancels it; the job leaves the open list.
  await page.unroute('**/api/v1/architecture/analyses/*/cancel');
  page.once('dialog', d => d.accept());
  await btn.click();
  await jobs.waitFor({state: 'detached'});
  assert.equal(posts, 2);
}));

test('cross-repository: inferred without a mapping, verified only after the user adds one', () => withApp({}, async (page, base) => {
  await page.goto(`${base}/#/architecture`);
  await page.locator('[data-cross]').click();
  const sum = page.locator('[data-testid="arch-cross-summary"]');
  await sum.waitFor();
  assert.match(await sum.innerText(), /0 verified · [1-9]\d* inferred/);
  assert.match(await page.locator('.arch-cross').innerText(), /Inferred \(name match only\)[\s\S]*fixturelib/);
  await shot(page, 'architecture-cross-repo.png');
  const form = page.locator('[data-mapform]');
  await form.locator('select[name=importer]').selectOption('github.com/example/fixture');
  await form.locator('input[name=package]').fill('fixturelib');
  await form.locator('select[name=provider]').selectOption('github.com/example/lib');
  await form.locator('button[type=submit]').click();
  await page.locator('[data-maps]').getByText('fixturelib').waitFor();
  await page.locator('[data-cross]').click();
  await page.getByText(/^[1-9]\d* verified · 0 inferred/).waitFor();
  assert.match(await page.locator('.arch-cross [data-xedge]').first().innerText(), /mapping/);
  await shot(page, 'architecture-cross-repo-verified.png');
}));

test('explain is labelled inferred, cites real edges and drops invented ones', () => withApp({}, async (page, base) => {
  await page.goto(`${base}/#/architecture`);
  await page.locator('[data-node="package:app"]').first().click();
  await page.locator('[data-open="module:app.util"]').click();
  await page.locator('[data-explain]').click();
  const box = page.locator('[data-testid="arch-explain"]');
  await box.waitFor();
  const t = await box.innerText();
  assert.match(t, /Inferred explanation/);
  assert.match(t, /source code not sent/);
  assert.match(t, /1 unverifiable citation\(s\) removed/);
  await shot(page, 'architecture-explain.png');
}));

test('explain button is absent when the capability is off', () => withApp({ARCH_MODE: 'failed'}, async (page, base) => {
  await page.goto(`${base}/#/architecture`);
  await page.locator('[data-node="package:app"]').first().click();
  await page.locator('[data-open="module:app.util"]').click();
  await page.locator('.arch-detail').waitFor();
  assert.equal(await page.locator('[data-explain]').count(), 0);
}));

test('API errors surface as an error state, not a blank page', () => withApp({}, async (page, base) => {
  await page.route('**/api/v1/architecture/snapshots?*', r => r.fulfill({status: 500, contentType: 'application/json', body: '{"error":"boom"}'}));
  await page.route('**/api/v1/architecture/snapshots', r => r.fulfill({status: 500, contentType: 'application/json', body: '{"error":"boom"}'}));
  await page.goto(`${base}/#/architecture`);
  await page.getByText(/boom|failed|error/i).first().waitFor();
}));

test('loading state is shown while the snapshot list is pending', () => withApp({}, async (page, base) => {
  let release;
  const gate = new Promise(r => { release = r; });
  await page.route('**/api/v1/architecture/snapshots**', async r => { await gate; await r.continue(); });
  await page.goto(`${base}/#/architecture`);
  await page.getByText(/loading/i).first().waitFor();
  release();
  await page.locator('[data-testid="arch-graph"]').waitFor();
}));
