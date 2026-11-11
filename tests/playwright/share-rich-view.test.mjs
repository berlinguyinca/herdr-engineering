// Real browsers (BROWSERS=chromium,firefox default; webkit opt-in) + real owner API + real share host: an opted-in report share frames the author's page inertly.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import * as playwright from 'playwright';

const ROOT = path.resolve(import.meta.dirname, '../..');
const PYTHON = process.env.PYTHON || (fs.existsSync(path.join(ROOT, '.venv/bin/python')) ? path.join(ROOT, '.venv/bin/python') : 'python3');
const WANTED = (process.env.BROWSERS || 'chromium,firefox').split(',').map(b => b.trim()).filter(Boolean);
const SHOTS = process.env.RICH_SHOT_DIR || '';

async function withShare(engine, callback, pageOptions = {}) {
  const server = spawn(PYTHON, [path.join(ROOT, 'tests/playwright/share-rich-server.py')], {
    cwd: ROOT, env: process.env, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let browser;
  try {
    const { port, link } = await new Promise((resolve, reject) => {
      let output = '';
      const timeout = setTimeout(() => reject(new Error('Share fixture did not start')), 30000);
      server.stdout.on('data', data => {
        output += data;
        const p = output.match(/PORT=(\d+)/); const l = output.match(/PATH=(\S+)/);
        if (p && l) { clearTimeout(timeout); resolve({ port: Number(p[1]), link: l[1] }); }
      });
      server.stderr.on('data', data => process.stderr.write(`[share-rich] ${data}`));
      server.once('exit', code => { clearTimeout(timeout); reject(new Error(`Share fixture exited ${code}`)); });
    });
    const options = { headless: true };
    if (engine === 'chromium') {
      options.args = ['--no-sandbox'];
      if (process.env.PLAYWRIGHT_CHROMIUM) options.executablePath = process.env.PLAYWRIGHT_CHROMIUM;
    }
    if (engine === 'webkit') {
      // NVIDIA EGL hosts crash WebKit's compositor; software Mesa renders the same pages.
      const mesa = '/usr/share/glvnd/egl_vendor.d/50_mesa.json';
      if (fs.existsSync(mesa)) options.env = { ...process.env, __EGL_VENDOR_LIBRARY_FILENAMES: mesa, LIBGL_ALWAYS_SOFTWARE: '1' };
    }
    try { browser = await playwright[engine].launch(options); } catch (error) { return { skipped: `${engine} is not installed: ${String(error.message).split('\n')[0]}` }; }
    const page = await browser.newPage(pageOptions);
    const console_ = [];
    page.on('console', m => console_.push(`${m.type()}: ${m.text()}`));
    await callback({ page, console: console_, url: `http://127.0.0.1:${port}${link}f/preview.html`, origin: `http://127.0.0.1:${port}` });
    return {};
  } finally {
    await browser?.close();
    server.kill();
  }
}

async function openRich(page, url) {
  await page.goto(url);
  const toggle = page.getByRole('button', { name: 'Show rich view', exact: true });
  await toggle.waitFor();
  assert.equal(await page.locator('iframe').count(), 0, 'no frame until asked');
  await toggle.click();
  const frameElement = page.locator('iframe[data-rich-frame]');
  await frameElement.waitFor();
  const frame = await (await frameElement.elementHandle()).contentFrame();
  await frame.getByRole('heading', { name: 'Candidate cuts p95 latency by 18%' }).waitFor();
  return { frameElement, frame };
}

const perEngine = (title, fn) => {
  for (const engine of WANTED) {
    test(`${engine}: ${title}`, async t => {
      const result = await fn(engine);
      if (result?.skipped) t.skip(result.skipped);
    });
  }
};

perEngine('the author page renders, its script does not run, and the frame cannot reach the share page', engine => withShare(engine, async ({ page, url, origin, console: logs }) => {
  const { frameElement, frame } = await openRich(page, url);
  assert.equal(await frameElement.getAttribute('sandbox'), '');
  assert.equal(await frameElement.getAttribute('src'), 'author-page.html');
  assert.equal(await page.locator('iframe').count(), 1);
  await frame.getByText('312 ms').waitFor();
  assert.equal(await frame.locator('#probe').textContent(), 'Scripts have not run.');
  assert.equal(await frame.evaluate(() => self.origin), 'null');
  const parentAccess = await frame.evaluate(() => { try { return typeof window.parent.document.title; } catch (error) { return error.name; } });
  assert.equal(parentAccess, 'SecurityError');
  assert.notEqual(await page.title(), 'pwned');
  const response = await page.request.get(new URL('author-page.html', url).href);
  assert.match(response.headers()['content-security-policy'], /^sandbox;/);
  assert.doesNotMatch(response.headers()['content-security-policy'], /allow-/);
  assert.doesNotMatch(response.headers()['content-security-policy'], /frame-ancestors/);
  assert.equal(response.headers()['x-frame-options'], undefined);
  await page.getByText('Scripts are disabled in this view', { exact: true }).waitFor();
  // the frame loaded under the preview's `frame-src 'self'`: no framing-related CSP/X-Frame error was logged (the sandbox script-block and favicon notices are expected)
  assert.deepEqual(logs.filter(l => /frame-src|frame-ancestors|refused to (display|frame)|x-frame-options|child-src/i.test(l)), []);
  // directly navigated, the page is still inert
  await page.goto(new URL('author-page.html', url).href);
  assert.equal(await page.locator('#probe').textContent(), 'Scripts have not run.');
  assert.ok(origin);
}));

perEngine('the preview is the default and the toggle hides the frame again', engine => withShare(engine, async ({ page, url }) => {
  await openRich(page, url);
  await page.getByRole('button', { name: 'Hide rich view', exact: true }).click();
  assert.equal(await page.locator('iframe').count(), 0);
}));

const box = async locator => { const b = await locator.boundingBox(); assert.ok(b, 'element has a box'); return b; };

for (const scheme of ['light', 'dark']) {
  perEngine(`phone 390x844 ${scheme}: no horizontal scroll, the toggle fits and is at least 24x24, tap and keyboard operate it`, engine => withShare(engine, async ({ page, url }) => {
    await page.goto(url);
    const toggle = page.locator('[data-rich-toggle]');
    await toggle.waitFor();
    const fits = async () => page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, inner: window.innerWidth }));
    let m = await fits();
    assert.ok(m.scroll <= m.inner, `page scrolls horizontally before opening: ${m.scroll} > ${m.inner}`);
    let b = await box(toggle);
    assert.ok(b.x >= 0 && b.x + b.width <= 390, `toggle outside viewport: ${JSON.stringify(b)}`);
    assert.ok(b.width >= 24 && b.height >= 24, `toggle target too small: ${b.width}x${b.height}`);
    await toggle.tap();                                             // touch
    const frameEl = page.locator('iframe[data-rich-frame]');
    await frameEl.waitFor();
    const frame = await (await frameEl.elementHandle()).contentFrame();
    await frame.getByRole('heading', { name: 'Candidate cuts p95 latency by 18%' }).waitFor();
    m = await fits();
    assert.ok(m.scroll <= m.inner, `page scrolls horizontally with the frame: ${m.scroll} > ${m.inner}`);
    const fb = await box(frameEl);
    assert.ok(fb.x >= 0 && fb.x + fb.width <= 390, `frame outside viewport: ${JSON.stringify(fb)}`);
    const note = page.locator('[data-rich-note]');
    const nb = await box(note);
    assert.ok(nb.x >= 0 && nb.x + nb.width <= 390, 'note outside viewport');
    assert.equal(await toggle.textContent(), 'Hide rich view');
    b = await box(toggle);
    assert.ok(b.width >= 24 && b.height >= 24);
    await toggle.focus();                                           // keyboard
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('iframe').count(), 0);
    await page.keyboard.press('Space');
    await frameEl.waitFor();
    assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
    if (SHOTS) { await frameEl.scrollIntoViewIfNeeded(); await page.screenshot({ path: path.join(SHOTS, `share-rich-${engine}-${scheme}-390.png`) }); }
  }, { viewport: { width: 390, height: 844 }, colorScheme: scheme, hasTouch: true }));
}
