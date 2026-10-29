// Shared density and responsive shell integration tests.
import { test } from 'node:test';
import assert from 'node:assert';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';
const hf=async p=>{await p.locator('[data-health-filters]').waitFor({state:'attached',timeout:15000});await p.evaluate(()=>{const d=document.querySelector('[data-health-filters]');if(d)d.open=true;});};  // the Health filter bar is a closed disclosure by default

// Density is an icon button that steps through the modes; click until it reads the wanted one.
async function setDensity(page, wanted) {
  const button = page.locator('herdr-app-shell [data-density-button]');
  await button.waitFor({ timeout: 5000 });
  for (let i = 0; i < 3 && (await button.getAttribute('data-value')) !== wanted; i += 1) await button.click();
  assert.equal(await button.getAttribute('data-value'), wanted);
}

const REPO = path.resolve(import.meta.dirname, '..', '..');
const PY = process.env.PYTHON
  || (fs.existsSync(path.join(REPO, '.venv', 'bin', 'python'))
    ? path.join(REPO, '.venv', 'bin', 'python') : 'python3');
async function startServer(env = {}, fixture = 'server.py') {
  const proc = spawn(PY, [path.join(REPO, 'tests', 'playwright', fixture)], {
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
  return chromium.launch({ headless: true, args });
}

// Start a fresh server + browser, run `fn`, then tear both down. A fresh
// server per test isolates the API rate budget (60s sliding window) so tests
// are deterministic.
async function withApp(fn, env = {}, fixture = 'server.py') {
  const srv = await startServer(env, fixture);
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

// On phones the display controls sit inside the Menu drawer; open it when it is closed.
async function openDisplayControls(page) {
  const menu = page.locator('herdr-app-shell .hd-shell__menu');
  if (await menu.isVisible() && (await menu.getAttribute('aria-expanded')) !== 'true') await menu.click();
}

test('density sanitizes storage, applies immediately by keyboard, and survives reload', async () => {
  await withApp(async ({ page, base }) => {
    await page.goto(`${base}/#/overview`);
    await page.evaluate(() => localStorage.setItem('herdr-density', '{invalid}'));
    await page.reload();
    await page.locator('herdr-app-shell [data-density-button]').waitFor({ timeout: 5000 });
    assert.equal(await page.locator('html').getAttribute('data-density'), 'comfortable');
    assert.equal(await page.evaluate(() => localStorage.getItem('herdr-density')), 'comfortable');
    const sizes = [];
    for (const value of ['comfortable', 'compact', 'dense']) {
      await setDensity(page, value);
      sizes.push(await page.locator('.ov-panel').first().evaluate(el => parseFloat(getComputedStyle(el).paddingTop)));
    }
    assert.ok(sizes[0] > sizes[1] && sizes[1] > sizes[2], `density panel spacing: ${sizes}`);
    await setDensity(page, 'comfortable');
    await page.locator('herdr-app-shell [data-density-button]').focus();
    await page.keyboard.press('Enter');   // a focused button steps to the next density
    assert.equal(await page.locator('html').getAttribute('data-density'), 'compact');
    await page.reload();
    await page.locator('herdr-app-shell [data-density-button]').waitFor({ timeout: 5000 });
    assert.equal(await page.locator('herdr-app-shell [data-density-button]').getAttribute('data-value'), 'compact');
    assert.equal(await page.evaluate(() => localStorage.getItem('herdr-density')), 'compact');
  }, { BURST: '0' });
});

test('density preserves mobile controls and representative route containment at all workspace widths', async () => {
  await withApp(async ({ page, base }) => {
    for (const width of [3840, 1920, 768, 390]) {
      await page.setViewportSize({ width, height: 1080 });
      for (const route of ['/overview', '/github', '/ci', '/services', '/analytics']) {
        await page.goto(`${base}/#${route}`);
        await openDisplayControls(page);
        await setDensity(page, 'dense');
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
          assert.ok((await page.locator('herdr-app-shell [data-density-button]').boundingBox()).height >= 44);
          if ((await menu.getAttribute('aria-expanded')) !== 'true') await menu.click();
          assert.ok((await page.getByRole('link', { name: 'Overview', exact: true }).first().boundingBox()).height >= 44);
        }
      }
    }
  }, { BURST: '0' });
});


test('populated tablet heatmap keeps touch targets within their own bucket columns', async () => {
  await withApp(async ({ page, base }) => {
    await page.setViewportSize({ width: 768, height: 1080 });
    await page.goto(`${base}/#/ci`);
    await page.locator('.ci-health-cell').first().waitFor({ timeout: 45000 });
    for (const width of [768, 701, 900, 390]) {
      await page.setViewportSize({ width, height: 1080 });
      for (const mode of ['14', '30']) {
        await hf(page);
        await page.locator(`[data-health-mode="${mode}"]`).click();
        for (const density of ['comfortable', 'compact', 'dense']) {
          await openDisplayControls(page);
          await setDensity(page, density);
          // Query and measure inside one browser task: CI history refreshes
          // periodically and replaces the heatmap subtree in place.
          const geometry = await page.evaluate(() => {
            const table = document.querySelector('[data-health-map] .ci-health-grid');
            const cells = [...table.querySelectorAll('tbody tr:first-child td')];
            const scroll = table.closest('[data-health-grid-scroll]');
            return {
              count: cells.length,
              widths: cells.map(cell => cell.getBoundingClientRect().width),
              contained: cells.every(cell => {
                const bucket = cell.getBoundingClientRect();
                const button = cell.querySelector('button').getBoundingClientRect();
                return button.width >= 44 && button.height >= 44 && button.left >= bucket.left && button.right <= bucket.right;
              }),
              internalScroll: scroll.scrollWidth > scroll.clientWidth,
              documentContained: document.documentElement.scrollWidth <= innerWidth,
            };
          });
          assert.equal(geometry.count, Number(mode));
          assert.ok(geometry.contained, `${width}px/${mode} days/${density}: targets exceed bucket columns ${geometry.widths}`);
          assert.ok(geometry.internalScroll, `${width}px/${mode} days: heatmap scrolls internally`);
          assert.ok(geometry.documentContained, `${width}px/${mode} days: document stays contained`);
        }
      }
    }
  }, {}, 'ci-health-server.py');
});
