// Real-browser check that a report prints / saves as PDF cleanly: print media is light in every theme, chrome and
// controls are gone, findings and figures are never split across pages, nothing is clipped, outbound URLs are spelled out.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { engineTest, launch } from './helpers/engines.mjs';

const ROOT = path.resolve(import.meta.dirname, '../..');
const PYTHON = process.env.PYTHON || (fs.existsSync(path.join(ROOT, '.venv/bin/python')) ? path.join(ROOT, '.venv/bin/python') : 'python3');
const SHOTS = process.env.REPORT_SHOT_DIR || '';
const FINDINGS = 14;

const PROGRAM = `
import json, sys
from herdr_engineering.control_plane.artifact_report_render import render_report_preview as r
n = int(sys.argv[1])
svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="600" height="1900" viewBox="0 0 600 1900">'
       '<rect width="600" height="1900" fill="#e8f1ff"/><rect x="20" y="20" width="560" height="1860" fill="none" stroke="#123" stroke-width="6"/>'
       '<text x="40" y="80" font-size="40">TALL-FIGURE-TOP</text><text x="40" y="1840" font-size="40">TALL-FIGURE-BOTTOM</text></svg>').encode()
report = {"schema_version": 1, "title": "Print check " + "long title " * 6, "summary": "A summary for the printed copy.",
  "questions": [{"id": "q1", "text": "What changed between the runs?"}],
  "evidence": [{"id": "e%d" % i, "title": "Evidence %d" % i, "description": "Observed values for run %d. " % i * 3, "asset_ids": ["data"]} for i in range(30)],
  "findings": [{"id": "f%d" % i, "title": "Finding number %d" % i, "statement": ("Statement %d is a sentence about the result. " % i) * 12,
                "question_ids": ["q1"], "evidence_ids": ["e%d" % i], "figure_ids": ["chart"],
                "limitations": ["Limit %d" % i], "next_steps": ["END-MARK-%d" % i]} for i in range(n)],
  "figures": [{"id": "chart", "title": "Tall chart", "asset_id": "fig", "caption": "A very tall figure.", "reading_guide": "Read top to bottom."}],
  "assets": [{"id": "data", "name": "evidence.csv", "role": "data", "media_type": "text/csv", "sha256": "a" * 64, "size_bytes": 1000},
             {"id": "fig", "name": "chart.svg", "role": "figure", "media_type": "image/svg+xml", "sha256": "b" * 64, "size_bytes": len(svg)}],
  "analysis": {"method": "METHOD-INSIDE-DETAILS", "configuration": {"baseline": "current"}},
  "limitations": ["LAST-LIMITATION"], "next_steps": ["Do the next check"]}
sys.stdout.write(r(report, figure_images={"fig": ("image/svg+xml", svg)}))
`;
const html = execFileSync(PYTHON, ['-c', PROGRAM, String(FINDINGS)], { cwd: ROOT, env: { ...process.env, PYTHONPATH: ROOT } }).toString();

const pdfPages = (file) => {
  const info = execFileSync('pdfinfo', [file]).toString();
  const total = Number(/Pages:\s+(\d+)/.exec(info)[1]);
  return Array.from({ length: total }, (_, i) => execFileSync('pdftotext', ['-f', String(i + 1), '-l', String(i + 1), '-layout', file, '-']).toString());
};
const shot = async (page, name, options = {}) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, name), ...options }); };

for (const [scheme, forced] of [['light', null], ['dark', null], ['dark', 'dark'], ['light', 'dark']]) {
  engineTest(`prints light, chrome-free and unsplit (os=${scheme}, forced=${forced})`, async (engine) => {
    const browser = await launch(engine);
    try {
      const page = await browser.newPage({ colorScheme: scheme, viewport: { width: 1000, height: 900 } });
      await page.setContent(html);
      if (forced) await page.evaluate(t => document.documentElement.setAttribute('data-theme', t), forced);
      // what a host adds after the header: an id-less panel (the opt-in Rich view) and an outbound link
      await page.evaluate(() => {
        const panel = document.createElement('section'); panel.className = 'card'; panel.setAttribute('data-rich-view', ''); panel.textContent = 'RICH-PANEL';
        document.querySelector('header.hero').after(panel);
        const link = document.createElement('a'); link.href = 'https://example.org/docs/page'; link.textContent = 'external docs'; link.id = 'ext';
        document.querySelector('.lede').append(' ', link);
      });
      if (SHOTS) { await shot(page, `screen-${scheme}-${forced}.png`, { fullPage: false }); }
      await page.emulateMedia({ media: 'print', colorScheme: scheme });
      const state = await page.evaluate(() => {
        const rp = document.querySelector('.rp'); const cs = getComputedStyle(rp);
        const shown = selector => [...document.querySelectorAll(selector)].filter(el => getComputedStyle(el).display !== 'none').length;
        return {
          bg: getComputedStyle(document.body).backgroundColor, rpBg: cs.backgroundColor, color: cs.color, scheme: cs.colorScheme,
          toc: shown('.toc'), buttons: shown('.btn'), panel: shown('[data-rich-view]'),
          after: getComputedStyle(document.getElementById('ext'), '::after').content,
          linkColor: getComputedStyle(document.getElementById('ext')).color,
          overflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          wrapOverflow: getComputedStyle(document.querySelector('.tablewrap')).overflowX,
          figMax: getComputedStyle(document.querySelector('.fig-img')).maxHeight,
        };
      });
      assert.equal(state.bg, 'rgb(255, 255, 255)'); assert.equal(state.rpBg, 'rgb(255, 255, 255)');
      assert.equal(state.color, 'rgb(0, 0, 0)'); assert.equal(state.scheme, 'light');
      assert.deepEqual([state.toc, state.buttons, state.panel], [0, 0, 0], 'no chrome in print');
      assert.match(state.after, /https:\/\/example\.org\/docs\/page/, 'an outbound link spells out its URL');
      assert.equal(state.linkColor, 'rgb(0, 0, 0)');
      assert.ok(state.overflowX <= 0, `no horizontal overflow: ${state.overflowX}`);
      assert.equal(state.wrapOverflow, 'visible'); assert.notEqual(state.figMax, 'none');

      if (engine !== 'chromium') return; // page.pdf() is Chromium-only; the print-media assertions above ran on this engine
      const file = path.join(os.tmpdir(), `report-print-${scheme}-${forced}-${process.pid}.pdf`);
      await page.pdf({ path: file, format: 'A4', printBackground: false, margin: { top: '14mm', bottom: '14mm', left: '12mm', right: '12mm' } });
      try {
        const pages = pdfPages(file);
        assert.ok(pages.length >= 4 && pages.length <= 40, `a multi-page document, not one clipped page or runaway: ${pages.length}`);
        const all = pages.join('\n');
        for (const needle of ['METHOD-INSIDE-DETAILS', 'LAST-LIMITATION', 'Finding number 0', `END-MARK-${FINDINGS - 1}`, 'https://example.org/docs/page'])
          assert.ok(all.includes(needle), `${needle} is printed (details open, nothing clipped)`);
        assert.ok(!all.includes('RICH-PANEL') && !all.includes('Download figure'), 'chrome absent from the PDF');
        // a finding is never split: its title and its last line are on the same page
        for (let i = 0; i < FINDINGS; i += 1) {
          const title = pages.findIndex(text => text.includes(`Finding number ${i}\n`) || new RegExp(`Finding number ${i}\\s*$`, 'm').test(text));
          const end = pages.findIndex(text => text.includes(`END-MARK-${i}`));
          assert.ok(title >= 0 && end >= 0, `finding ${i} present`);
          assert.equal(title, end, `finding ${i} is on one page (title page ${title + 1}, end page ${end + 1})`);
        }
        // the 1900px-tall figure fits within one page rather than being cut: the whole card shares one page
        const top = pages.findIndex(text => text.includes('Tall chart'));
        assert.ok(top >= 0);
        assert.ok(pages[top].includes('Read top to bottom.'), 'the figure card (title, image, caption, reading guide) is not split');
        if (SHOTS) for (const n of [1, 2]) execFileSync('pdftoppm', ['-png', '-r', '60', '-f', String(n), '-l', String(n), file, path.join(SHOTS, `pdf-${scheme}-${forced}-p${n}`)]);
      } finally { fs.rmSync(file, { force: true }); }
    } finally { await browser.close(); }
  });
}

engineTest('a report inside the dashboard preview wrapper prints in full, not clipped to the 640px scroll box', async (engine) => {
  const { PRINT_CSS } = await import('../../packages/artifact-static-preview.js');
  const browser = await launch(engine);
  try {
    const page = await browser.newPage({ colorScheme: 'dark', viewport: { width: 1000, height: 900 } });
    await page.setContent('<div data-static-preview style="contain:paint;overflow:auto;max-height:640px"><div id="h" data-theme="dark"></div></div>');
    await page.addStyleTag({ content: PRINT_CSS });
    await page.evaluate(markup => {
      document.getElementById('h').attachShadow({ mode: 'open' }).innerHTML = markup.replace(/<!doctype html>|<\/?(html|head|body|meta)[^>]*>|<title>.*?<\/title>/g, '');
    }, html);
    await page.emulateMedia({ media: 'print', colorScheme: 'dark' });
    const probe = await page.evaluate(() => {
      const wrapper = document.querySelector('[data-static-preview]');
      const rp = document.getElementById('h').shadowRoot.querySelector('.rp');
      return { clipped: wrapper.getBoundingClientRect().height < rp.getBoundingClientRect().height - 1, bg: getComputedStyle(rp).backgroundColor, color: getComputedStyle(rp).color };
    });
    assert.equal(probe.clipped, false); assert.equal(probe.bg, 'rgb(255, 255, 255)'); assert.equal(probe.color, 'rgb(0, 0, 0)');
    if (engine !== 'chromium') return; // page.pdf() is Chromium-only
    const file = path.join(os.tmpdir(), `report-print-shadow-${process.pid}.pdf`);
    await page.pdf({ path: file, format: 'A4' });
    try { assert.ok(pdfPages(file).join('\n').includes(`END-MARK-${FINDINGS - 1}`), 'the last finding is in the PDF'); } finally { fs.rmSync(file, { force: true }); }
  } finally { await browser.close(); }
});
