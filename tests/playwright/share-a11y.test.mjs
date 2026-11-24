// Accessibility + layout sweep of the dashboard share panel (ARIA tabs, checkbox) and the share viewer's Rich view toggle,
// in real browsers (BROWSERS=chromium,firefox default; webkit opt-in). Fixtures: artifact-shares-server.py (dashboard)
// and share-rich-server.py (share host). SHOT_DIR=<dir> also writes screenshots (light/dark, desktop/390px).
//
//   PYTHON=<venv python> node --test tests/playwright/share-a11y.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import * as playwright from 'playwright';
import { staticAudit, focusIndicatorAudit, overflowAudit, overflowFailure, summarize } from './a11y-checks.mjs';

const ROOT = path.resolve(import.meta.dirname, '../..');
const PYTHON = process.env.PYTHON || (fs.existsSync(path.join(ROOT, '.venv/bin/python')) ? path.join(ROOT, '.venv/bin/python') : 'python3');
const WANTED = (process.env.BROWSERS || 'chromium,firefox').split(',').map(b => b.trim()).filter(Boolean);
const SHOTS = process.env.SHOT_DIR || '';
const BOX = '[data-share-field="includeAuthorHtml"]';

async function launch(engine) {
  const options = { headless: true };
  if (engine === 'chromium') {
    options.args = ['--no-sandbox'];
    if (process.env.PLAYWRIGHT_CHROMIUM) options.executablePath = process.env.PLAYWRIGHT_CHROMIUM;
  }
  if (engine === 'webkit') {
    const mesa = '/usr/share/glvnd/egl_vendor.d/50_mesa.json';
    if (fs.existsSync(mesa)) options.env = { ...process.env, __EGL_VENDOR_LIBRARY_FILENAMES: mesa, LIBGL_ALWAYS_SOFTWARE: '1' };
  }
  return playwright[engine].launch(options);
}

async function withServer(script, pattern, engine, callback, pageOptions = {}) {
  const server = spawn(PYTHON, [path.join(ROOT, 'tests/playwright', script)], { cwd: ROOT, env: process.env, stdio: ['ignore', 'pipe', 'pipe'] });
  let browser;
  try {
    const out = await new Promise((resolve, reject) => {
      let output = '';
      const timeout = setTimeout(() => reject(new Error(`${script} did not start`)), 30000);
      server.stdout.on('data', data => { output += data; if (pattern.test(output)) { clearTimeout(timeout); resolve(output); } });
      server.stderr.on('data', data => process.stderr.write(`[${script}] ${data}`));
      server.once('exit', code => { clearTimeout(timeout); reject(new Error(`${script} exited ${code}`)); });
    });
    try { browser = await launch(engine); } catch (error) { return { skipped: `${engine} is not installed: ${String(error.message).split('\n')[0]}` }; }
    const page = await browser.newPage(pageOptions);
    await callback({ page, out, port: Number(out.match(/PORT=(\d+)/)[1]) });
    return {};
  } finally {
    await browser?.close();
    server.kill();
  }
}

const dashboard = (engine, cb, opts) => withServer('artifact-shares-server.py', /PORT=\d+/, engine, ({ page, port }) => cb({ page, base: `http://127.0.0.1:${port}` }), opts);
const viewer = (engine, cb, opts) => withServer('share-rich-server.py', /PORT=\d+[\s\S]*PATH=\S+/, engine,
  ({ page, out, port }) => cb({ page, url: `http://127.0.0.1:${port}${out.match(/PATH=(\S+)/)[1]}f/preview.html` }), opts);

const perEngine = (title, fn) => {
  for (const engine of WANTED) {
    test(`${engine}: ${title}`, async t => {
      const result = await fn(engine);
      if (result?.skipped) t.skip(result.skipped);
    });
  }
};

const panel = page => page.locator('[data-share-panel]');
async function openReport(page, base) {
  await page.goto(`${base}/#/artifacts/report_1`);
  await panel(page).waitFor();
  await panel(page).locator('[data-share-list-region]').filter({ hasNotText: 'Loading links…' }).waitFor();
}
const shot = (page, name) => SHOTS ? page.screenshot({ path: path.join(SHOTS, name), fullPage: true }) : null;
const tabs = page => page.locator('[data-panel-tabs] [role=tab]');

perEngine('share panel tabs follow the ARIA tabs pattern: one tab stop, arrows/Home/End move and select, panels are labelled', engine => dashboard(engine, async ({ page, base }) => {
  await openReport(page, base);
  const keys = ['links', 'collaborators', 'activity', 'feedback'];
  const state = () => page.evaluate(() => [...document.querySelectorAll('[data-panel-tabs] [role=tab]')].map(t => ({ sel: t.getAttribute('aria-selected'), tab: t.tabIndex, controls: t.getAttribute('aria-controls'), id: t.id })));
  let s = await state();
  assert.deepEqual(s.map(t => t.tab), [0, -1, -1, -1], 'roving tabindex: only the selected tab is a tab stop');
  for (const [i, t] of s.entries()) {
    assert.ok(t.id && t.controls, `tab ${i} needs id and aria-controls`);
    const pane = page.locator(`#${t.controls}`);
    assert.equal(await pane.getAttribute('role'), 'tabpanel');
    assert.equal(await pane.getAttribute('aria-labelledby'), t.id);
  }
  await tabs(page).first().focus();
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.evaluate(() => document.activeElement.dataset.panelTab), 'collaborators');
  assert.equal(await page.evaluate(() => document.activeElement.getAttribute('aria-selected')), 'true');
  await page.keyboard.press('End');
  assert.equal(await page.evaluate(() => document.activeElement.dataset.panelTab), 'feedback');
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.evaluate(() => document.activeElement.dataset.panelTab), 'links', 'wraps');
  await page.keyboard.press('ArrowLeft');
  assert.equal(await page.evaluate(() => document.activeElement.dataset.panelTab), 'feedback', 'wraps back');
  await page.keyboard.press('Home');
  assert.equal(await page.evaluate(() => document.activeElement.dataset.panelTab), 'links');
  await page.keyboard.press('ArrowRight');
  await page.locator('[data-panel-pane="collaborators"]').filter({ hasNotText: /^$/ }).waitFor();   // the lazily mounted pane has settled
  s = await state();
  assert.deepEqual(s.map(t => t.sel), ['false', 'true', 'false', 'false']);
  assert.deepEqual(s.map(t => t.tab), [-1, 0, -1, -1]);
  assert.equal(await page.locator('[data-panel-pane="links"]').isHidden(), true);
  // Tab never steps to a sibling tab (they are not tab stops); with nothing focusable after the panel the browser may keep focus here.
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement.dataset.panelTab ?? 'left'), await page.evaluate(() => document.activeElement.dataset.panelTab ? 'collaborators' : 'left'));
  assert.ok(keys.length === 4);
}));

for (const scheme of ['light', 'dark']) {
  perEngine(`share panel ${scheme}: every control shows a focus indicator, the checkbox is named, text contrast passes`, engine => dashboard(engine, async ({ page, base }) => {
    await openReport(page, base);
    await panel(page).locator('[data-share-field="audience"]').selectOption('public');
    await panel(page).locator('[data-share-field="versionId"]').selectOption('report_v2');
    const box = panel(page).locator(BOX);
    await box.waitFor();
    assert.equal(await box.evaluate(e => e.labels?.length), 1, 'the checkbox has one label');
    assert.equal(await box.evaluate(e => e.getAttribute('aria-describedby') && document.getElementById(e.getAttribute('aria-describedby'))?.textContent.length > 0), true);
    const focus = await focusIndicatorAudit(page, '[data-artifact-share]');
    assert.ok(focus.stats.focused >= 6, `looked at ${focus.stats.focused} controls`);
    assert.equal(focus.length, 0, summarize(focus));
    const stat = await staticAudit(page, '[data-artifact-share]');
    assert.equal(stat.length, 0, summarize(stat));
    await shot(page, `dashboard-${engine}-${scheme}-desktop.png`);
  }, { colorScheme: scheme, viewport: { width: 1280, height: 900 } }));

  perEngine(`share panel ${scheme} at 390px: no horizontal scroll`, engine => dashboard(engine, async ({ page, base }) => {
    await openReport(page, base);
    await panel(page).locator('[data-share-field="audience"]').selectOption('public');
    await panel(page).locator('[data-share-field="versionId"]').selectOption('report_v2');
    await panel(page).locator(BOX).waitFor();
    const o = await overflowAudit(page);
    o.clipped = o.clipped.filter(c => !c.startsWith('herdr-freshness'));   // the page's own hidden freshness element, not the share panel
    assert.equal(overflowFailure(o), '', JSON.stringify(o).slice(0, 400));
    await shot(page, `dashboard-${engine}-${scheme}-390.png`);
  }, { colorScheme: scheme, viewport: { width: 390, height: 844 } }));

  perEngine(`share viewer ${scheme}: Rich view toggle is named, stateful, focus-visible and readable`, engine => viewer(engine, async ({ page, url }) => {
    await page.goto(url);
    const toggle = page.getByRole('button', { name: 'Show rich view', exact: true });
    await toggle.waitFor();
    assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
    const focus = await focusIndicatorAudit(page, 'body');
    assert.ok(focus.stats.focused >= 1);
    assert.equal(focus.length, 0, summarize(focus));
    const stat = await staticAudit(page, 'body');
    assert.equal(stat.length, 0, summarize(stat));
    await shot(page, `viewer-${engine}-${scheme}-closed.png`);
    await toggle.click();
    await page.locator('iframe[data-rich-frame]').waitFor();
    const frame = page.locator('iframe[data-rich-frame]');
    assert.ok(await frame.getAttribute('title'));
    assert.equal(await page.getByRole('button', { name: 'Hide rich view', exact: true }).getAttribute('aria-expanded'), 'true');
    assert.equal(await page.getByRole('button', { name: 'Hide rich view', exact: true }).getAttribute('aria-controls') !== null, true, 'toggle names the region it controls');
    const after = await staticAudit(page, 'body');
    assert.equal(after.length, 0, summarize(after));
    await shot(page, `viewer-${engine}-${scheme}-open.png`);
  }, { colorScheme: scheme, viewport: { width: 1280, height: 900 } }));

  perEngine(`share viewer ${scheme} at 390px: no horizontal scroll`, engine => viewer(engine, async ({ page, url }) => {
    await page.goto(url);
    await page.getByRole('button', { name: 'Show rich view', exact: true }).click();
    await page.locator('iframe[data-rich-frame]').waitFor();
    const o = await overflowAudit(page);
    assert.equal(overflowFailure(o), '', JSON.stringify(o).slice(0, 400));
    await shot(page, `viewer-${engine}-${scheme}-390.png`);
  }, { colorScheme: scheme, viewport: { width: 390, height: 844 } }));
}
