// Real-browser check of finding anchors: every finding/section heading has a keyboard-reachable "#" permalink, a URL
// with #fragment scrolls to and highlights its target on the share page (CSS :target only: the page has no script),
// and in the dashboard preview (a shadow root, where :target cannot work) the client wires the same behaviour.
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
from herdr_engineering.control_plane import artifact_render_api as api
from herdr_engineering.control_plane.artifact_report_render import render_report_preview as r
report = {"schema_version": 1, "title": "Anchors check", "summary": "Summary.",
  "questions": [{"id": "q1", "text": "What changed?"}],
  "evidence": [{"id": "e%d" % i, "title": "Evidence %d" % i, "description": "Observed values %d. " % i * 4, "asset_ids": ["data"]} for i in range(12)],
  "findings": [{"id": "f%d" % i, "title": ('Hostile <img src=x onerror=alert(1)> "q" & title' if i == 3 else "Finding number %d" % i),
                "statement": ("Statement %d is a sentence about the result. " % i) * 14, "question_ids": ["q1"], "evidence_ids": ["e%d" % i],
                "limitations": ["Limit %d" % i], "next_steps": ["Next %d" % i]} for i in range(12)],
  "figures": [], "assets": [{"id": "data", "name": "evidence.csv", "role": "data", "media_type": "text/csv", "sha256": "a" * 64, "size_bytes": 1000}],
  "limitations": ["Last limitation"], "next_steps": ["Do the next check"]}
page = r(report)
dash = api._envelope("html", page.encode(), api.dashboard_resolver(), None)["html"]
sys.stdout.write(json.dumps({"share": page, "dash": dash}))
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
const inView = (page, selector) => page.evaluate(sel => { const r = document.querySelector(sel).getBoundingClientRect(); return r.top >= 0 && r.top < innerHeight; }, selector);

for (const [scheme, width, height, tag] of [['light', 1100, 800, 'desktop'], ['dark', 1100, 800, 'desktop'], ['light', 390, 800, 'phone'], ['dark', 390, 800, 'phone']]) {
  engineTest(`share page: #fragment scrolls to and highlights the finding (${scheme}, ${tag})`, async (engine) => {
    const browser = await launch(engine);
    try {
      const page = await browser.newPage({ colorScheme: scheme, viewport: { width, height } });
      await page.goto(`${base}/report.html#finding-f9`);
      assert.ok(await inView(page, '#finding-f9'), 'the target is scrolled into view');
      const style = await page.evaluate(() => { const cs = getComputedStyle(document.getElementById('finding-f9')); return { outline: cs.outlineStyle, width: cs.outlineWidth, bg: cs.backgroundColor }; });
      assert.equal(style.outline, 'solid'); assert.notEqual(style.width, '0px');
      const other = await page.evaluate(() => getComputedStyle(document.getElementById('finding-f2')).outlineStyle);
      assert.equal(other, 'none', 'only the target is highlighted');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth) <= 0, true, 'no horizontal overflow');
      await shot(page, `anchors-share-${scheme}-${tag}.png`);
    } finally { await browser.close(); }
  });
}

engineTest('share page: every permalink is a real link to an existing id, reachable and operable by keyboard', async (engine) => {
  const browser = await launch(engine);
  try {
    const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
    await page.goto(`${base}/report.html`);
    const audit = await page.evaluate(() => [...document.querySelectorAll('a.anchor')].map(a => ({
      ok: document.getElementById(a.getAttribute('href').slice(1)) !== null && document.getElementById(a.getAttribute('aria-describedby')) !== null, label: a.getAttribute('aria-label'), w: a.getBoundingClientRect().width, h: a.getBoundingClientRect().height,
    })));
    assert.ok(audit.length >= 14, `permalinks found: ${audit.length}`);
    for (const item of audit) { assert.ok(item.ok, 'target id exists'); assert.match(item.label, /^Link to this (finding|figure|section)$/); assert.ok(item.w >= 24 && item.h >= 24, `target size ${item.w}x${item.h}`); }
    // Tab to the first permalink, see a focus ring, press Enter, land on the target.
    for (let i = 0; i < 60; i += 1) { await page.keyboard.press('Tab'); if (await page.evaluate(() => document.activeElement?.classList.contains('anchor'))) break; }
    assert.equal(await page.evaluate(() => document.activeElement.classList.contains('anchor')), true, 'a permalink takes keyboard focus');
    const ring = await page.evaluate(() => getComputedStyle(document.activeElement).outlineStyle);
    assert.equal(ring, 'solid', 'focus is visible');
    const href = await page.evaluate(() => document.activeElement.getAttribute('href'));
    await page.keyboard.press('Enter');
    assert.equal(new URL(page.url()).hash, href);
    const hostile = await page.evaluate(() => { const a = document.querySelector('#finding-f3 .anchor'); return [a.getAttribute('aria-label'), document.getElementById(a.getAttribute('aria-describedby')).textContent]; });
    assert.equal(hostile[0], 'Link to this finding', 'no free text in an attribute');
    assert.match(hostile[1], /Hostile <img src=x onerror=alert\(1\)> "q" & title/, 'the hostile title is shown as text, never markup');
    assert.equal(await page.evaluate(() => document.querySelectorAll('img[src="x"]').length), 0, 'no injected element');
  } finally { await browser.close(); }
});

for (const [scheme, width, tag] of [['light', 1100, 'desktop'], ['dark', 1100, 'desktop'], ['light', 390, 'phone'], ['dark', 390, 'phone']]) {
  engineTest(`dashboard preview: permalink click and ?at= deep link reveal the section inside the shadow root (${scheme}, ${tag})`, async (engine) => {
    const browser = await launch(engine);
    try {
      const page = await browser.newPage({ colorScheme: scheme, viewport: { width, height: 800 } });
      await page.goto(`${base}/dash.html`);
      await page.evaluate(async html => {
        const { mountStaticHtml } = await import('/packages/artifact-static-preview.js');
        window.navigated = [];
        mountStaticHtml(document.getElementById('out'), html, { label: 'Report preview', focus: 'finding-f8', permalink: id => `${location.origin}/dash.html#/artifacts/a?at=${id}`, onNavigate: (id, url) => window.navigated.push([id, url]) });
      }, built.dash);
      const state = () => page.evaluate(() => {
        const shadow = document.querySelector('[data-static-preview] > div').shadowRoot;
        const wrapper = document.querySelector('[data-static-preview]');
        const marked = [...shadow.querySelectorAll('.is-target')].map(n => n.id);
        const active = shadow.activeElement?.id ?? null;
        return { marked, active, scrollTop: wrapper.scrollTop, anchors: shadow.querySelectorAll('a.anchor').length, hrefs: [...shadow.querySelectorAll('a.anchor')].every(a => a.getAttribute('href')) };
      });
      let s = await state();
      assert.deepEqual(s.marked, ['finding-f8'], 'the deep-link target is highlighted on mount');
      assert.equal(s.active, 'finding-f8', 'and holds focus');
      assert.ok(s.scrollTop > 0, 'the preview scrolled to it');
      assert.ok(s.anchors >= 14 && s.hrefs, 'every permalink was given a real href');
      await page.evaluate(() => document.querySelector('[data-static-preview] > div').shadowRoot.querySelector('#finding-f1 a.anchor').click());
      s = await state();
      assert.deepEqual(s.marked, ['finding-f1'], 'a click moves the highlight');
      assert.deepEqual(await page.evaluate(() => window.navigated.map(n => n[0])), ['finding-f1']);
      assert.equal(new URL(page.url()).hash, '', 'the click never drove the hash router');
      await shot(page, `anchors-dashboard-${scheme}-${tag}.png`);
    } finally { await browser.close(); }
  });
}
