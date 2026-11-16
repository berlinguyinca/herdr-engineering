// Phone-width and dark-theme polish of the dashboard report viewer and its share panel, in real browsers
// (BROWSERS=chromium,firefox default). Fixture: artifact-shares-server.py (real dashboard routes, in-memory registry).
//
//   PYTHON=<venv python> node --test tests/playwright/viewer-phone-polish.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { launch, ENGINES } from './helpers/engines.mjs';

const ROOT = path.resolve(import.meta.dirname, '../..');
const PYTHON = process.env.PYTHON || (fs.existsSync(path.join(ROOT, '.venv/bin/python')) ? path.join(ROOT, '.venv/bin/python') : 'python3');
const SHOTS = process.env.SHOT_DIR || '';

async function withDashboard(engine, options, callback) {
  const server = spawn(PYTHON, [path.join(ROOT, 'tests/playwright/artifact-shares-server.py')], { cwd: ROOT, env: process.env, stdio: ['ignore', 'pipe', 'pipe'] });
  let browser;
  try {
    const out = await new Promise((resolve, reject) => {
      let output = '';
      const timeout = setTimeout(() => reject(new Error('fixture did not start')), 30000);
      server.stdout.on('data', data => { output += data; if (/PORT=\d+/.test(output)) { clearTimeout(timeout); resolve(output); } });
      server.stderr.on('data', data => process.stderr.write(`[fixture] ${data}`));
      server.once('exit', code => { clearTimeout(timeout); reject(new Error(`fixture exited ${code}`)); });
    });
    browser = await launch(engine);
    const page = await browser.newPage(options);
    await page.goto(`http://127.0.0.1:${out.match(/PORT=(\d+)/)[1]}/#/artifacts/report_1`);
    await page.locator('[data-share-panel] [data-share-list-region]').filter({ hasNotText: 'Loading links…' }).waitFor();
    await page.locator('[data-report-feedback-form]').waitFor();
    await page.locator('[data-share-panel] [data-share-field="audience"]').selectOption('public');
    await callback(page);
  } finally { await browser?.close(); server.kill(); }
}

const each = (title, options, fn) => {
  for (const engine of ENGINES) {
    test(`${engine}: ${title}`, async t => {
      try { (await launch(engine)).close(); } catch (e) { return t.skip(`${engine} is not installed`); }
      await withDashboard(engine, options, page => fn(page, engine));
    });
  }
};
const PHONE = { viewport: { width: 390, height: 844 } };
const shot = (page, name) => SHOTS ? page.screenshot({ path: path.join(SHOTS, name), fullPage: true }) : null;

each('the downloads table is stacked into labelled blocks at 390px, nothing clipped', { ...PHONE }, async (page, engine) => {
  const t = await page.evaluate(() => {
    const table = document.querySelector('[data-artifact-detail] .hd-table');
    const wrap = table.closest('.hd-table-wrap');
    const vw = document.documentElement.clientWidth;
    return { display: getComputedStyle(table).display, scrolls: wrap.scrollWidth > wrap.clientWidth + 1,
      maxRight: Math.max(...[...table.querySelectorAll('td')].map(td => td.getBoundingClientRect().right)), vw,
      labels: [...table.querySelector('tbody tr').children].map(td => getComputedStyle(td, '::before').content.replace(/"/g, '')),
      roles: [table.getAttribute('role'), table.querySelector('tbody tr').getAttribute('role'), table.querySelector('td').getAttribute('role')] };
  });
  assert.equal(t.display, 'block');
  assert.equal(t.scrolls, false);
  assert.ok(t.maxRight <= t.vw, `${t.maxRight} <= ${t.vw}`);
  assert.deepEqual(t.labels, ['Asset', 'Role', 'Format', 'Size', 'Integrity', 'Download']);
  assert.deepEqual(t.roles, ['table', 'row', 'cell']);
  await shot(page, `viewer-${engine}-390.png`);
});

each('the downloads table is a normal table on desktop', { viewport: { width: 1100, height: 800 } }, async page => {
  assert.equal(await page.evaluate(() => getComputedStyle(document.querySelector('[data-artifact-detail] .hd-table')).display), 'table');
});

each('share panel tabs stay on one row at 390px', PHONE, async (page, engine) => {
  const tops = await page.evaluate(() => [...document.querySelectorAll('[data-panel-tabs] [role=tab]')].map(t => Math.round(t.getBoundingClientRect().top)));
  assert.equal(new Set(tops).size, 1, `tab tops ${tops}`);
  assert.equal(await page.evaluate(() => { const b = document.querySelector('[data-panel-tabs]'); return b.scrollWidth <= b.clientWidth + 1; }), true);
  await shot(page, `panel-${engine}-390.png`);
});

each('every label leaves a gap before its control, and the Links heading and Title have top margin', PHONE, async page => {
  const bad = await page.evaluate(() => {
    const out = [];
    for (const label of document.querySelectorAll('[data-artifact-detail] label')) {
      const control = label.querySelector(':scope > select, :scope > textarea, :scope > input:not([type=checkbox])');
      if (!control) continue;
      const text = [...label.childNodes].find(n => n.nodeType === 3 && n.textContent.trim());
      const range = document.createRange(); range.selectNodeContents(text);
      const gap = control.getBoundingClientRect().top - range.getBoundingClientRect().bottom;
      if (gap < 3) out.push(`${text.textContent.trim()}: ${Math.round(gap)}px`);
    }
    return out;
  });
  assert.deepEqual(bad, []);
  const margins = await page.evaluate(() => ({
    links: parseFloat(getComputedStyle(document.querySelector('[data-share-panel] > h3')).marginTop),
    title: (() => { const l = document.querySelector('[data-share-field="title"]').closest('label'); return l.getBoundingClientRect().top - l.previousElementSibling.getBoundingClientRect().bottom; })() }));
  assert.ok(margins.links >= 12, `Links margin ${margins.links}`);
  assert.ok(margins.title >= 8, `Title gap ${margins.title}`);
});

for (const scheme of ['light', 'dark']) {
  each(`${scheme}: fields and the evidence fieldset use the theme tokens`, { ...PHONE, colorScheme: scheme }, async (page, engine) => {
    const r = await page.evaluate(() => {
      const probe = document.createElement('span'); document.body.append(probe);
      const resolve = (prop, v) => { probe.style[prop] = `var(${v})`; return getComputedStyle(probe)[prop]; };
      const cs = sel => getComputedStyle(document.querySelector(sel));
      const surface = resolve('backgroundColor', '--surface'), border = resolve('borderTopColor', '--border');
      probe.style.borderTopStyle = 'solid';
      const b = resolve('borderTopColor', '--border');
      return { surface, border: b,
        textarea: [cs('[data-report-feedback-body]').backgroundColor, cs('[data-report-check-notes]').backgroundColor, cs('[data-share-field="title"]').backgroundColor],
        fieldset: cs('[data-report-check-form] fieldset').borderTopColor, field: cs('[data-report-feedback-body]').borderTopColor };
    });
    for (const bg of r.textarea) assert.equal(bg, r.surface);
    assert.equal(r.fieldset, r.border);
    assert.equal(r.field, r.border);
    await shot(page, `tokens-${engine}-${scheme}-390.png`);
  });
}
