// Real-browser check of the report preview's light/dark behaviour: an explicit data-theme (set by the dashboard on the
// shadow host, or on :root when the page stands alone) wins over the OS colour scheme; with none, the OS decides.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const ROOT = path.resolve(import.meta.dirname, '../..');
const PYTHON = process.env.PYTHON || (fs.existsSync(path.join(ROOT, '.venv/bin/python')) ? path.join(ROOT, '.venv/bin/python') : 'python3');
const REPORT = {
  schema_version: 1, title: 'Theme check', summary: 's', questions: [{ id: 'q-expect', text: 'q' }], evidence: [], findings: [],
  figures: [], assets: [],
};
const html = execFileSync(PYTHON, ['-c',
  'import json,sys;from herdr_engineering.control_plane.artifact_report_render import render_report_preview as r;'
  + 'sys.stdout.write(r(json.loads(sys.argv[1])))', JSON.stringify(REPORT)], { cwd: ROOT, env: { ...process.env, PYTHONPATH: ROOT } }).toString();
const LIGHT = 'rgb(247, 247, 248)', DARK = 'rgb(23, 24, 28)';
const CASES = [['light', null, LIGHT], ['dark', null, DARK], ['light', 'dark', DARK], ['dark', 'light', LIGHT], ['dark', 'dark', DARK], ['light', 'light', LIGHT]];

test('explicit theme beats the OS scheme, standalone and inside a shadow root', async () => {
  const options = { headless: true, args: ['--no-sandbox'] };
  if (process.env.PLAYWRIGHT_CHROMIUM) options.executablePath = process.env.PLAYWRIGHT_CHROMIUM;
  const browser = await chromium.launch(options);
  try {
    for (const [os, forced, expected] of CASES) {
      const page = await browser.newPage({ colorScheme: os });
      await page.setContent(html);
      if (forced) await page.evaluate(t => document.documentElement.setAttribute('data-theme', t), forced);
      assert.equal(await page.evaluate(() => getComputedStyle(document.querySelector('.rp')).backgroundColor), expected, `standalone os=${os} forced=${forced}`);
      await page.setContent('<div id="h"></div>');
      await page.evaluate(([markup, t]) => {
        const host = document.getElementById('h');
        if (t) host.setAttribute('data-theme', t);
        host.attachShadow({ mode: 'open' }).innerHTML = markup.replace(/<!doctype html>|<\/?(html|head|body|meta)[^>]*>|<title>.*?<\/title>/g, '');
      }, [html, forced]);
      assert.equal(await page.evaluate(() => getComputedStyle(document.getElementById('h').shadowRoot.querySelector('.rp')).backgroundColor), expected, `shadow os=${os} forced=${forced}`);
      await page.close();
    }
  } finally { await browser.close(); }
});

test('question ids stay on one line as pills', async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'], ...(process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {}) });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 800 } });
    await page.setContent(html);
    const box = await page.evaluate(() => { const r = document.querySelector('.qlist .chip').getBoundingClientRect(); return { w: r.width, h: r.height }; });
    assert.ok(box.h < 28 && box.w > box.h, JSON.stringify(box));
  } finally { await browser.close(); }
});
