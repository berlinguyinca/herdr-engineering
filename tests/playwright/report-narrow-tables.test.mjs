// Real-browser check that the Evidence and Downloads tables are readable at phone width without scrolling inside the
// table: below 600px each row is a labelled block (real <table> markup and roles kept), on desktop it is a normal table,
// and print keeps the table with its repeating header. Covers the raw preview, the real public sanitiser, and the
// dashboard's shadow-root copy, in light and dark.
import { after } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { engineTest, launch } from './helpers/engines.mjs';

const ROOT = path.resolve(import.meta.dirname, '../..');
const PYTHON = process.env.PYTHON || (fs.existsSync(path.join(ROOT, '.venv/bin/python')) ? path.join(ROOT, '.venv/bin/python') : 'python3');
const SHOTS = process.env.REPORT_SHOT_DIR || '';

const PROGRAM = `
import json, sys
from herdr_engineering.control_plane import share_render as sr
from herdr_engineering.control_plane.artifact_report_render import render_report_preview as r
report = {"schema_version": 1, "title": "Narrow tables check", "summary": "A summary.",
  "questions": [{"id": "q1", "text": "What changed?"}],
  "evidence": [{"id": "e%d" % i, "title": "Evidence %d with a reasonably long title" % i, "description": "Observed values for run %d. " % i * 4,
                "asset_ids": ["data", "table-with-a-long-identifier"]} for i in range(4)],
  "findings": [{"id": "f1", "title": "Finding", "statement": "A statement. " * 6, "question_ids": ["q1"], "evidence_ids": ["e1"],
                "limitations": ["A limit"], "next_steps": ["A next check"]}],
  "figures": [],
  "assets": [{"id": "data", "name": "evidence-with-a-rather-long-file-name.csv", "role": "data", "media_type": "text/csv", "sha256": "a" * 64, "size_bytes": 209715200},
             {"id": "table-with-a-long-identifier", "name": "summary.json", "role": "data", "media_type": "application/json", "sha256": "b" * 64, "size_bytes": 2048}],
  "limitations": ["A limitation"], "next_steps": ["Do the next check"]}
page = r(report)
public = sr.sanitize_html(page.encode(), resolve=lambda _c, url: url)
if isinstance(public, bytes): public = public.decode()
sys.stdout.write(json.dumps({"share": page, "public": public, "dash": page}))  # the dashboard report route (artifact_report_api preview) serves this page, stylesheet included
`;
const built = JSON.parse(execFileSync(PYTHON, ['-c', PROGRAM], { cwd: ROOT, env: { ...process.env, PYTHONPATH: ROOT } }).toString());

const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://x');
  const page = { '/report.html': built.share, '/public.html': built.public }[url.pathname];
  if (page) { res.setHeader('content-type', 'text/html; charset=utf-8'); return res.end(page); }
  if (url.pathname === '/dash.html') { res.setHeader('content-type', 'text/html; charset=utf-8'); return res.end('<!doctype html><meta charset=utf-8><body style="margin:0"><div id="out"></div>'); }
  const file = path.join(ROOT, url.pathname);
  if (url.pathname.startsWith('/packages/') && file.startsWith(ROOT) && fs.existsSync(file)) { res.setHeader('content-type', 'text/javascript'); return res.end(fs.readFileSync(file)); }
  res.statusCode = 404; res.end();
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;
after(() => server.close());

const shot = async (page, name) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, name), fullPage: true }); };

// Runs inside the page (or its shadow root): measures every table cell against the visible width.
const measure = (rootSelector) => {
  const root = rootSelector ? document.querySelector(rootSelector).shadowRoot : document;
  const vw = document.documentElement.clientWidth;
  return [...root.querySelectorAll('table')].map(table => {
    const wrap = table.closest('.tablewrap');
    const cells = [...table.querySelectorAll('tbody td')];
    const label = td => getComputedStyle(td, '::before').content;
    return {
      kind: table.className, display: getComputedStyle(table).display, wrapScrolls: wrap.scrollWidth > wrap.clientWidth + 1,
      maxRight: Math.max(...cells.map(td => td.getBoundingClientRect().right)), vw,
      cells: cells.length, firstRowLabels: [...table.querySelector('tbody tr').children].map(label),
      headDisplay: getComputedStyle(table.querySelector('thead')).display, roles: [table.getAttribute('role'), table.querySelector('tbody tr').getAttribute('role'), table.querySelector('td').getAttribute('role')],
      headerRoles: [...table.querySelectorAll('th')].map(th => th.getAttribute('role')),
    };
  });
};

for (const [pageName, route] of [['preview', '/report.html'], ['public sanitiser', '/public.html']]) {
  for (const [scheme, width, height, tag] of [['light', 390, 844, 'phone'], ['dark', 390, 844, 'phone'], ['light', 1100, 800, 'desktop'], ['dark', 1100, 800, 'desktop']]) {
    engineTest(`${pageName}: Evidence and Downloads tables fit without inner scrolling (${scheme}, ${tag})`, async (engine) => {
      const browser = await launch(engine);
      try {
        const page = await browser.newPage({ colorScheme: scheme, viewport: { width, height } });
        await page.goto(`${base}${route}`);
        const tables = await page.evaluate(measure, null);
        assert.equal(tables.length, 2);
        assert.deepEqual(tables.map(t => t.kind), ['t-evidence', 't-downloads']);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth) <= 0, true, 'no page-level horizontal overflow');
        for (const t of tables) {
          assert.equal(t.wrapScrolls, false, `${t.kind}: no sideways scroll inside the wrapper (${engine}, ${tag})`);
          assert.ok(t.maxRight <= t.vw, `${t.kind}: every cell ends inside the viewport: ${t.maxRight} <= ${t.vw}`);
          assert.deepEqual(t.roles, ['table', 'row', 'cell'], 'table semantics are kept explicitly');
          assert.ok(t.headerRoles.every(r => r === 'columnheader'));
          if (tag === 'phone') {
            assert.equal(t.display, 'block', 'rows are stacked');
            assert.deepEqual(t.firstRowLabels.map(l => l.replace(/"/g, '')), t.kind === 't-evidence' ? ['Evidence', 'Description', 'Files'] : ['Name', 'Role', 'Type', 'Size', 'SHA-256'], 'each value is labelled');
            assert.notEqual(t.headDisplay, 'none', 'the header stays in the accessibility tree');
          } else {
            assert.equal(t.display, 'table', 'a real table on desktop');
            assert.deepEqual(t.firstRowLabels, new Array(t.firstRowLabels.length).fill('none'), 'no stacked labels on desktop');
          }
        }
        await shot(page, `narrow-${route === '/report.html' ? 'preview' : 'public'}-${engine}-${scheme}-${tag}.png`);
      } finally { await browser.close(); }
    });
  }
}

engineTest('stacked rows keep their #evidence anchors working at 390px', async (engine) => {
  const browser = await launch(engine);
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    await page.goto(`${base}/report.html#evidence-e3`);
    const state = await page.evaluate(() => { const r = document.getElementById('evidence-e3').getBoundingClientRect(); return { top: r.top, outline: getComputedStyle(document.querySelector('#evidence-e3 td')).backgroundColor }; });
    assert.ok(state.top >= 0 && state.top < 844, `the evidence row is scrolled into view: ${state.top}`);
  } finally { await browser.close(); }
});

engineTest('print keeps the real table with a repeating header, not the stacked layout', async (engine) => {
  const browser = await launch(engine);
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    await page.goto(`${base}/report.html`);
    await page.emulateMedia({ media: 'print' });
    const t = await page.evaluate(measure, null);
    for (const table of t) assert.equal(table.display, 'table');
    assert.equal(await page.evaluate(() => getComputedStyle(document.querySelector('thead')).display), 'table-header-group');
    assert.deepEqual(t[0].firstRowLabels, ['none', 'none', 'none']);
  } finally { await browser.close(); }
});

for (const [scheme, width, tag] of [['light', 390, 'phone'], ['dark', 390, 'phone'], ['light', 1100, 'desktop'], ['dark', 1100, 'desktop']]) {
  engineTest(`dashboard preview: the tables stack inside the shadow root (${scheme}, ${tag})`, async (engine) => {
    const browser = await launch(engine);
    try {
      const page = await browser.newPage({ colorScheme: scheme, viewport: { width, height: 844 } });
      await page.goto(`${base}/dash.html`);
      await page.evaluate(async html => {
        const { mountStaticHtml } = await import('/packages/artifact-static-preview.js');
        mountStaticHtml(document.getElementById('out'), html, { label: 'Report preview' });
      }, built.dash);
      const tables = await page.evaluate(measure, '[data-static-preview] > div');
      assert.equal(tables.length, 2);
      for (const t of tables) {
        assert.equal(t.wrapScrolls, false, `${t.kind}: no sideways scroll inside the wrapper`);
        assert.equal(t.display, tag === 'phone' ? 'block' : 'table');
        if (tag === 'phone') assert.deepEqual(t.roles, ['table', 'row', 'cell']);
      }
      await shot(page, `narrow-dashboard-${engine}-${scheme}-${tag}.png`);
    } finally { await browser.close(); }
  });
}
