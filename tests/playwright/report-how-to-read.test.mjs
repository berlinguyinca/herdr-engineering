// Real-browser check of the "How to read this report" legend: collapsed by default, operable by keyboard, readable at
// 390px without horizontal scroll, light and dark, left out of print, and intact in the dashboard's shadow-root copy.
import { after } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { engineTest, launch } from './helpers/engines.mjs';

const ROOT = path.resolve(import.meta.dirname, '../..');
const PYTHON = process.env.PYTHON || (fs.existsSync(path.join(ROOT, '.venv/bin/python')) ? path.join(ROOT, '.venv/bin/python') : 'python3');
const SHOTS = process.env.REPORT_SHOT_DIR || '';

const PROGRAM = `
import json, sys
from herdr_engineering.control_plane import artifact_render_api as api
from herdr_engineering.control_plane.artifact_report_render import render_report_preview as r
report = {"schema_version": 1, "title": "Legend check", "summary": "A summary of the report.",
  "questions": [{"id": "q1", "text": "What changed?"}],
  "evidence": [{"id": "e1", "title": "Evidence 1", "description": "Observed values.", "asset_ids": ["data"]}],
  "findings": [{"id": "f%d" % i, "title": "Finding number %d" % i, "statement": "A statement about the result. " * 6, "question_ids": ["q1"],
                "evidence_ids": ["e1"], "figure_ids": ["chart"], "limitations": ["A limit"], "next_steps": ["A next check"]} for i in range(3)],
  "figures": [{"id": "chart", "title": "A chart", "asset_id": "data", "caption": "Units: seconds.", "reading_guide": "Read left to right."}],
  "assets": [{"id": "data", "name": "evidence.csv", "role": "data", "media_type": "text/csv", "sha256": "a" * 64, "size_bytes": 1000}],
  "analysis": {"method": "A method"}, "limitations": ["A limitation"], "next_steps": ["Do the next check"]}
page = r(report)
sys.stdout.write(json.dumps({"share": page, "dash": api._envelope("html", page.encode(), api.dashboard_resolver(), None)["html"]}))
`;
const built = JSON.parse(execFileSync(PYTHON, ['-c', PROGRAM], { cwd: ROOT, env: { ...process.env, PYTHONPATH: ROOT } }).toString());

const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://x');
  if (url.pathname === '/report.html') { res.setHeader('content-type', 'text/html; charset=utf-8'); return res.end(built.share); }
  if (url.pathname === '/dash.html') { res.setHeader('content-type', 'text/html; charset=utf-8'); return res.end('<!doctype html><meta charset=utf-8><body style="margin:0"><div id="out"></div>'); }
  const file = path.join(ROOT, url.pathname);
  if (url.pathname.startsWith('/packages/') && file.startsWith(ROOT) && fs.existsSync(file)) { res.setHeader('content-type', 'text/javascript'); return res.end(fs.readFileSync(file)); }
  res.statusCode = 404; res.end();
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;
after(() => server.close());

const shot = async (page, name) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, name) }); };

for (const [scheme, width, tag] of [['light', 1100, 'desktop'], ['dark', 1100, 'desktop'], ['light', 390, 'phone'], ['dark', 390, 'phone']]) {
  engineTest(`share page: legend is collapsed, keyboard-operable and fits (${scheme}, ${tag})`, async (engine) => {
    const browser = await launch(engine);
    try {
      const page = await browser.newPage({ colorScheme: scheme, viewport: { width, height: 800 } });
      await page.goto(`${base}/report.html`);
      const legend = page.locator('details.howto');
      assert.equal(await legend.evaluate(el => el.open), false, 'collapsed by default');
      assert.equal(await legend.locator('dl').isVisible(), false, 'its content is hidden while collapsed');
      assert.equal(await page.locator('main > header ~ details, header details.howto').count(), 1);
      await shot(page, `howto-collapsed-${scheme}-${tag}.png`);
      // keyboard: Tab to the summary, see a focus ring, Enter opens, Space closes
      for (let i = 0; i < 40; i += 1) { await page.keyboard.press('Tab'); if (await page.evaluate(() => document.activeElement?.matches('details.howto > summary'))) break; }
      assert.equal(await page.evaluate(() => document.activeElement.matches('details.howto > summary')), true, 'the summary takes keyboard focus');
      assert.equal(await page.evaluate(() => getComputedStyle(document.activeElement).outlineStyle), 'solid', 'focus is visible');
      await page.keyboard.press('Enter');
      assert.equal(await legend.evaluate(el => el.open), true);
      assert.equal(await legend.locator('dl').isVisible(), true);
      const terms = await legend.locator('dt').allTextContents();
      for (const term of ['Finding', 'Limits', 'Next checks', 'How to read', 'Severity, confidence']) assert.ok(terms.some(t => t.includes(term)), `${term} is explained`);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth) <= 0, true, 'no horizontal overflow when open');
      await shot(page, `howto-open-${scheme}-${tag}.png`);
      await page.keyboard.press('Space');
      assert.equal(await legend.evaluate(el => el.open), false, 'Space closes it again');
    } finally { await browser.close(); }
  });
}

engineTest('print: the legend is left out, the report is not', async (engine) => {
  const browser = await launch(engine);
  try {
    const page = await browser.newPage({ viewport: { width: 1000, height: 900 } });
    await page.goto(`${base}/report.html`);
    await page.emulateMedia({ media: 'print' });
    assert.equal(await page.locator('details.howto').isVisible(), false, 'hidden on paper');
    if (engine !== 'chromium') return; // page.pdf() is Chromium-only; the print-media check above ran on this engine
    const pdf = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'howto-')), 'r.pdf');
    await page.pdf({ path: pdf });
    const text = execFileSync('pdftotext', ['-layout', pdf, '-']).toString();
    assert.ok(text.includes('Finding number 0') && text.includes('A limitation'), 'the report itself is printed');
    assert.ok(!text.includes('How to read this report') && !text.includes('Severity, confidence'), 'the legend is not');
  } finally { await browser.close(); }
});

engineTest('dashboard preview: the legend survives the sanitiser, collapsed, and toggles inside the shadow root', async (engine) => {
  const browser = await launch(engine);
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 800 } });
    await page.goto(`${base}/dash.html`);
    await page.evaluate(async html => {
      const { mountStaticHtml } = await import('/packages/artifact-static-preview.js');
      mountStaticHtml(document.getElementById('out'), html, { label: 'Report preview' });
    }, built.dash);
    const state = () => page.evaluate(() => { const d = document.querySelector('[data-static-preview] > div').shadowRoot.querySelector('details.howto'); return { found: !!d, open: d?.open, dts: d ? d.querySelectorAll('dt').length : 0 }; });
    let s = await state();
    assert.equal(s.found, true); assert.equal(s.open, false); assert.ok(s.dts >= 6);
    await page.evaluate(() => document.querySelector('[data-static-preview] > div').shadowRoot.querySelector('details.howto > summary').click());
    assert.equal((await state()).open, true);
    await shot(page, 'howto-dashboard-phone.png');
  } finally { await browser.close(); }
});
