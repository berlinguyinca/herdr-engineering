import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import {chromium} from 'playwright';

const root = path.resolve(import.meta.dirname, '../..');
const python = process.env.PYTHON || (fs.existsSync(path.join(root, '.venv/bin/python')) ? path.join(root, '.venv/bin/python') : 'python3');
const shots = path.join(root, 'docs/specs/herdr-codeboarding-native-port-2026-10-23/evidence');

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
  await page.locator('[data-diff]').selectOption({index: 1});
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
    const bg = await page.locator('.arch').evaluate(e => getComputedStyle(e).backgroundColor);
    if (ctx.colorScheme === 'dark') assert.equal(bg, 'rgb(22, 24, 29)'); else assert.equal(bg, 'rgb(255, 255, 255)');
    // headings must actually be readable on the view's own surface (WCAG AA 4.5:1)
    const lum = c => { const [r, g, b] = c.match(/\d+/g).slice(0, 3).map(Number).map(v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }); return .2126 * r + .7152 * g + .0722 * b; };
    const ratio = (x, y) => { const [hi, lo] = [lum(x), lum(y)].sort((m, n) => n - m); return (hi + .05) / (lo + .05); };
    for (const sel of ['.arch h1', '.arch .arch-meta', '.arch-table td']) {
      const fg = await page.locator(sel).first().evaluate(e => getComputedStyle(e).color);
      assert.ok(ratio(fg, bg) >= 4.5, `${sel} contrast ${ratio(fg, bg).toFixed(2)} on ${name}: ${fg} vs ${bg}`);
    }
    const nodeFill = await page.locator('.arch-node rect').first().evaluate(e => getComputedStyle(e).fill);
    const nodeText = await page.locator('.arch-node .t1').first().evaluate(e => getComputedStyle(e).fill);
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
