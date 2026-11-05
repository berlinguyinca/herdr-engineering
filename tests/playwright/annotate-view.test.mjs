// Real-browser regression for the annotation view: hostile content CSS must not cover, restyle or script our mode bar.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { chromium } from 'playwright';

const ROOT = path.resolve(import.meta.dirname, '../..');
const PYTHON = process.env.PYTHON || (fs.existsSync(path.join(ROOT, '.venv/bin/python')) ? path.join(ROOT, '.venv/bin/python') : 'python3');
const POINTS = [[0.5, 0.5], [0.15, 0.2], [0.85, 0.8]];

async function launch(args) {
  const options = { headless: true, args: ['--no-sandbox', ...args] };
  if (process.env.PLAYWRIGHT_CHROMIUM) options.executablePath = process.env.PLAYWRIGHT_CHROMIUM;
  return chromium.launch(options);
}

async function withView(callback) {
  const hits = [];
  const listener = http.createServer((req, res) => { hits.push(req.url); res.end(); });
  await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
  const server = spawn(PYTHON, [path.join(ROOT, 'tests/playwright/annotate-view-server.py'), String(listener.address().port)],
    { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] });
  let browser, buffer = '';
  try {
    const info = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('annotation server did not start')), 30000);
      server.stdout.on('data', d => { buffer += d; try { const v = JSON.parse(buffer.split('\n')[0]); clearTimeout(timer); resolve(v); } catch { /* wait */ } });
      server.stderr.on('data', d => process.stderr.write(`[annotate-server] ${d}`));
      server.once('exit', code => { clearTimeout(timer); reject(new Error(`server exited ${code}`)); });
    });
    browser = await launch([`--host-resolver-rules=MAP share.example 127.0.0.1:${info.port}`]);
    const context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 900, height: 700 } });
    await callback({ context, info, hits });
  } finally {
    await browser?.close();
    server.kill();
    listener.close();
  }
}

for (const file of ['host.html', 'abs.html', 'xform.html']) {
  test(`hostile CSS in ${file} cannot cover our mode bar, run script or reach out`, async () => {
    await withView(async ({ context, info, hits }) => {
      const page = await context.newPage();
      const dialogs = [], consoles = [];
      page.on('dialog', d => { dialogs.push(d.message()); d.dismiss(); });
      page.on('console', m => consoles.push(m.text()));
      await page.goto(`https://share.example/s/${info.token}/k/${info.session}/a/${file}`);
      await page.waitForSelector('nav[data-mode-switch]');
      await page.waitForTimeout(500);
      assert.equal(await page.evaluate(() => getComputedStyle(document.querySelector('nav[data-mode-switch]')).zIndex), '20',
        'our stylesheet must have loaded under the page policy');
      for (const sel of ['a[data-mode=live]', 'a[data-mode=annotate]']) {
        const box = await page.locator(`nav ${sel}`).boundingBox();
        for (const [fx, fy] of POINTS) {
          const hit = await page.evaluate(([x, y]) => {
            const el = document.elementFromPoint(x, y);
            return el && el.closest('nav[data-mode-switch]') ? 'nav' : (el ? `${el.tagName}#${el.id}` : 'null');
          }, [box.x + box.width * fx, box.y + box.height * fy]);
          assert.equal(hit, 'nav', `${file} ${sel} at ${fx},${fy}`);
        }
      }
      // the content's fixed boxes stay inside their own box: the page far below it is not the content
      const below = await page.evaluate(() => {
        const el = document.elementFromPoint(450, 650);
        return el && el.closest('#herdr-viewport') ? 'content' : 'page';
      });
      assert.equal(below, 'page', `${file}: content must not paint over the rest of the page`);
      assert.deepEqual(dialogs, []);
      assert.equal(await page.evaluate(() => window.__x), undefined);
      assert.deepEqual(consoles.filter(l => /alert|__x/.test(l)), []);
      assert.deepEqual(hits, [], 'no request may reach the external listener');
    });
  });
}

for (const mode of ['v', 'a']) {
  test(`at 320px a long unbroken line of ${mode}/wide.txt can be reached by scrolling the content box`, async () => {
    await withView(async ({ context, info }) => {
      const page = await context.newPage();
      await page.setViewportSize({ width: 320, height: 640 });
      await page.goto(`https://share.example/s/${info.token}/k/${info.session}/${mode}/wide.txt`);
      await page.waitForSelector('#herdr-viewport');
      const reach = await page.evaluate(async () => {
        const box = document.querySelector('#herdr-viewport');
        const text = box.querySelector('#herdr-content').shadowRoot.querySelector('pre');
        const before = box.scrollLeft;
        box.scrollLeft = 400;
        await new Promise(r => requestAnimationFrame(r));
        return { before, after: box.scrollLeft, wide: !!text && text.scrollWidth > box.clientWidth, page: document.documentElement.scrollWidth - innerWidth };
      });
      assert.equal(reach.wide, true, 'the test text must be wider than the box');
      assert.ok(reach.after > 0, `content box must scroll sideways (scrollLeft ${reach.before} -> ${reach.after})`);
      assert.ok(reach.page <= 0, 'the page itself must not scroll sideways');
    });
  });
}

test('annotation view text colours keep 4.5:1 contrast in light and dark schemes', async () => {
  await withView(async ({ context, info }) => {
    const page = await context.newPage();
    const luminance = ([r, g, b]) => [r, g, b].map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; })
      .reduce((a, v, i) => a + v * [0.2126, 0.7152, 0.0722][i], 0);
    for (const scheme of ['light', 'dark']) {
      await page.emulateMedia({ colorScheme: scheme });
      await page.goto(`https://share.example/s/${info.token}/k/${info.session}/a/wide.txt`);
      await page.waitForSelector('aside[data-annotate-panel]');
      const colours = await page.evaluate(() => {
        const css = getComputedStyle(document.documentElement);
        const probe = document.createElement('span');
        const read = value => { probe.style.color = value; document.body.append(probe); const c = getComputedStyle(probe).color; probe.remove(); return c; };
        const nums = c => c.match(/[\d.]+/g).slice(0, 3).map(Number);
        return { rawMuted: read(css.getPropertyValue('--muted').trim()), bg: nums(read('Canvas')), muted: nums(read(css.getPropertyValue('--muted').trim())), danger: nums(read(css.getPropertyValue('--danger').trim())) };
      });
      assert.ok(!colours.rawMuted.startsWith('rgba'), `muted must be opaque, not alpha-blended: ${colours.rawMuted}`);
      for (const name of ['muted', 'danger']) {
        const [hi, lo] = [luminance(colours[name]), luminance(colours.bg)].sort((a, b) => b - a);
        assert.ok((hi + 0.05) / (lo + 0.05) >= 4.5, `${scheme} ${name} ${colours[name]} on ${colours.bg}`);
      }
    }
  });
});
