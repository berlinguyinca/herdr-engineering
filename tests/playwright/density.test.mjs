// Shared density and responsive shell integration tests.
import { test } from 'node:test';
import assert from 'node:assert';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const REPO = path.resolve(import.meta.dirname, '..', '..');
const PY = process.env.PYTHON
  || (fs.existsSync(path.join(REPO, '.venv', 'bin', 'python'))
    ? path.join(REPO, '.venv', 'bin', 'python') : 'python3');
// Playwright resolves its own bundled browser (matching the pinned
// devDependency). On dev boxes whose cached browser build lags the installed
// Playwright version (this host has chromium-1243 while Playwright 1.64 wants
// headless_shell-1248), fall back to a locally-pinned chromium. Override with
// PLAYWRIGHT_CHROMIUM to force a specific executable.
const EXE_FALLBACK = process.env.PLAYWRIGHT_CHROMIUM
  || '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';


async function startServer(env = {}) {
  const proc = spawn(PY, [path.join(REPO, 'tests', 'playwright', 'server.py')], {
    env: { ...process.env, ...env },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const port = await new Promise((resolve, reject) => {
    let buf = '';
    const timer = setTimeout(() => reject(new Error('server start timeout')), 20000);
    proc.stdout.on('data', (d) => {
      buf += d.toString();
      let m;
      while ((m = buf.match(/PORT=(\d+)/))) {
        clearTimeout(timer);
        resolve(Number(m[1]));
        buf = buf.slice(m.index + m[0].length);
      }
    });
    proc.on('exit', (c) => reject(new Error(`server exited early (${c})`)));
    proc.stderr.on('data', (d) => process.stderr.write(`[srv] ${d}`));
  });
  return { proc, port };
}

async function launchBrowser() {
  const args = ['--no-sandbox'];
  if (process.env.PLAYWRIGHT_CHROMIUM) {
    return chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM, args });
  }
  try {
    return await chromium.launch({ headless: true, args }); // auto-detect bundled browser
  } catch (err) {
    if (fs.existsSync(EXE_FALLBACK)) {
      return chromium.launch({ headless: true, executablePath: EXE_FALLBACK, args });
    }
    throw err;
  }
}

// Start a fresh server + browser, run `fn`, then tear both down. A fresh
// server per test isolates the API rate budget (60s sliding window) so tests
// are deterministic.
async function withApp(fn, env = {}) {
  const srv = await startServer(env);
  let browser;
  try {
    browser = await launchBrowser();
    const page = await browser.newPage();
    const base = `http://127.0.0.1:${srv.port}`;
    return await fn({ page, base });
  } finally {
    if (browser) { try { await browser.close(); } catch { /* noop */ } }
    try { srv.proc.kill(); } catch { /* noop */ }
  }
}

test('density sanitizes storage, applies immediately by keyboard, and survives reload', async () => {
  await withApp(async ({ page, base }) => {
    await page.goto(`${base}/#/overview`);
    await page.evaluate(() => localStorage.setItem('herdr-density', '{invalid}'));
    await page.reload();
    const density = page.getByRole('combobox', { name: 'Density' });
    await density.waitFor({ timeout: 5000 });
    assert.equal(await page.locator('html').getAttribute('data-density'), 'comfortable');
    assert.equal(await page.evaluate(() => localStorage.getItem('herdr-density')), 'comfortable');
    const sizes = [];
    for (const value of ['comfortable', 'compact', 'dense']) {
      await density.selectOption(value);
      sizes.push(await page.locator('.ov-panel').first().evaluate(el => parseFloat(getComputedStyle(el).paddingTop)));
    }
    assert.ok(sizes[0] > sizes[1] && sizes[1] > sizes[2], `density panel spacing: ${sizes}`);
    await density.focus();
    await page.keyboard.press('Home');
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('html').getAttribute('data-density'), 'compact');
    await page.reload();
    await density.waitFor({ timeout: 5000 });
    assert.equal(await density.inputValue(), 'compact');
    assert.equal(await page.evaluate(() => localStorage.getItem('herdr-density')), 'compact');
  }, { BURST: '0' });
});

test('density preserves mobile controls and representative route containment at all workspace widths', async () => {
  await withApp(async ({ page, base }) => {
    for (const width of [3840, 1920, 768, 390]) {
      await page.setViewportSize({ width, height: 1080 });
      for (const route of ['/overview', '/github', '/ci', '/services', '/analytics']) {
        await page.goto(`${base}/#${route}`);
        const density = page.getByRole('combobox', { name: 'Density' });
        await density.selectOption('dense', { timeout: 5000 });
        await page.locator('#view h1').first().waitFor();
        if (route === '/github') await page.locator('.gh-layout').waitFor();
        await page.waitForTimeout(300);
        const geometry = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth }));
        assert.ok(geometry.scroll <= width + 1, `${route} at ${width}: document width ${geometry.scroll}`);
        await page.screenshot({ path: path.join(REPO, '.superpowers/sdd/2026-10-22-operations-workspace', `density-${width}-${route.slice(1)}.png`) });
        if (width <= 900) {
          const menu = page.getByRole('button', { name: 'Menu', exact: true });
          assert.ok(await menu.isVisible(), `collapsed sidebar at ${width}`);
          assert.ok((await menu.boundingBox()).height >= 44);
          assert.ok((await density.boundingBox()).height >= 44);
          await menu.click();
          assert.ok((await page.getByRole('link', { name: 'Overview', exact: true }).first().boundingBox()).height >= 44);
        }
      }
    }
  }, { BURST: '0' });
});
