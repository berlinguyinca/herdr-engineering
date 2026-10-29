// Shared helpers for the T16 UI gates: browser launch, thresholds (ui-gates.yaml), the real integrated server, and
// per-page watchers for console errors, CSP violations and failed requests.
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { chromium } from 'playwright';

export const REPO = path.resolve(import.meta.dirname, '..', '..');

/** Minimal indentation-based YAML reader: nested maps with scalar leaves (all ui-gates.yaml needs). */
export function loadConfig(file = path.join(import.meta.dirname, 'ui-gates.yaml')) {
  const root = {}; const stack = [[-1, root]];
  for (const raw of readFileSync(file, 'utf8').split('\n')) {
    const line = raw.replace(/\s+#.*$/, '').replace(/^#.*$/, '');
    if (!line.trim()) continue;
    const indent = line.length - line.trimStart().length;
    const m = /^(\S+?):\s*(.*)$/.exec(line.trim());
    if (!m) throw new Error(`ui-gates.yaml: cannot read "${raw}"`);
    while (stack.at(-1)[0] >= indent) stack.pop();
    const parent = stack.at(-1)[1];
    if (m[2] === '') { parent[m[1]] = {}; stack.push([indent, parent[m[1]]]); continue; }
    const v = m[2].replace(/^"(.*)"$/, '$1');
    parent[m[1]] = /^-?\d+(\.\d+)?$/.test(v) ? Number(v) : v;
  }
  return root;
}
export const CFG = loadConfig();

/** The browser Playwright itself resolves for this install (PLAYWRIGHT_BROWSERS_PATH is Playwright's own setting, not ours).
 *  No path is hard-coded: a machine without the browser gets Playwright's own "run npx playwright install" message. */
export const chromePath = () => chromium.executablePath();

/** The fontconfig the browser runs under: the bundled fonts and NOTHING from the host, including the host's /etc/fonts/conf.d.
 *  The pixel gate compares screenshots, and what Skia does to a glyph (hinting, anti-aliasing, sub-pixel order) comes from
 *  fontconfig: a CI host whose conf.d differs from the workstation the baselines were made on moved every one of the 88 pages by
 *  1-11% of its pixels. The values below are the ones the baselines were made with (Debian/Ubuntu 10-hinting-slight,
 *  10-sub-pixel-rgb, 10-yes-antialias, 11-lcdfilter-default); pinning them here is what makes a baseline independent of the host. */
let pinnedFontconfig;
export function pinnedFontconfigFile() {
  if (pinnedFontconfig) return pinnedFontconfig;
  const dir = mkdtempSync(path.join(os.tmpdir(), 'herdr-fc-'));
  pinnedFontconfig = path.join(dir, 'fonts.conf');
  writeFileSync(pinnedFontconfig, `<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd"><fontconfig>`
    + `<dir>${path.join(import.meta.dirname, 'fonts')}</dir><cachedir>${path.join(dir, 'cache')}</cachedir>`
    + '<match target="pattern">'
    + '<edit name="antialias" mode="assign"><bool>true</bool></edit>'
    + '<edit name="hinting" mode="assign"><bool>true</bool></edit>'
    + '<edit name="hintstyle" mode="assign"><const>hintslight</const></edit>'
    + '<edit name="rgba" mode="assign"><const>rgb</const></edit>'
    + '<edit name="lcdfilter" mode="assign"><const>lcddefault</const></edit>'
    + '</match></fontconfig>');
  return pinnedFontconfig;
}
export const launch = () => chromium.launch({
  headless: true, args: ['--no-sandbox'], env: { ...process.env, FONTCONFIG_FILE: pinnedFontconfigFile() },
});

/** Start tests/playwright/real_server.py (the integrated control plane on loopback). Resolves with its handshake line. */
export async function startRealServer(cfg = {}) {
  const py = existsSync(path.join(REPO, '.venv', 'bin', 'python')) ? path.join(REPO, '.venv', 'bin', 'python') : 'python3';
  const proc = spawn(py, [path.join(import.meta.dirname, 'real_server.py'), JSON.stringify(cfg)], { stdio: ['ignore', 'pipe', 'pipe'] });
  let err = '';
  proc.stderr.on('data', (d) => { err += d; });
  const info = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`real server did not start: ${err.slice(-800)}`)), cfg.timeout || 180000);
    let buf = '';
    proc.stdout.on('data', (d) => { buf += d; const i = buf.indexOf('\n'); if (i >= 0) { clearTimeout(timer); try { resolve(JSON.parse(buf.slice(0, i))); } catch (e) { reject(e); } } });
    proc.on('exit', (c) => { clearTimeout(timer); reject(new Error(`real server exited ${c}: ${err.slice(-800)}`)); });
  });
  // SIGTERM first so the server removes its temp directory; SIGKILL only if it has not left after 5 s. The server also leaves by
  // itself when this process dies (parent-death watchdog in real_server.py).
  let gone = proc.exitCode !== null || proc.signalCode !== null;
  proc.on('exit', () => { gone = true; });
  const stop = () => {
    if (gone) return;
    proc.kill('SIGTERM');
    const t = setTimeout(() => { if (!gone) proc.kill('SIGKILL'); }, 5000); t.unref();
  };
  return { info, stop, exited: () => gone };
}

/** Collects everything that counts as "the page is unhealthy": console errors/warnings, page errors, CSP violations, failed requests. */
export function watch(page) {
  const w = { errors: [], csp: [], failed: [], bad: [] };
  page.on('console', (m) => { if (['error', 'warning'].includes(m.type())) w.errors.push(`${m.type()}: ${m.text()}`); });
  page.on('pageerror', (e) => w.errors.push(`pageerror: ${e.message}`));
  page.on('requestfailed', (r) => w.failed.push(`${r.method()} ${r.url()} ${r.failure()?.errorText}`));
  page.on('response', (r) => { if (r.status() >= 400) w.bad.push(`${r.status()} ${r.request().method()} ${r.url()}`); });
  page.addInitScript(() => { document.addEventListener('securitypolicyviolation', (e) => { (window.__csp ||= []).push(`${e.violatedDirective} ${e.blockedURI}`); }); });
  w.collectCsp = async () => { try { w.csp = await page.evaluate(() => window.__csp || []); } catch { /* page gone */ } return w.csp; };
  return w;
}

/** How much to stretch waits on a loaded host: the 1-minute load average per CPU, clamped to 1..3. Playwright's 30 s default is right on
 *  an idle machine and too tight on a CI host shared with other jobs (2026-10-28: 6 of 268 tests timed out waiting for the room to open
 *  while the host ran at 17 min instead of 11, and the same commit passed on a re-run). Only waits stretch, so a passing run costs
 *  nothing; a real failure under load takes at most 3x longer to report. */
export function loadScale(load = os.loadavg()[0], cpus = os.cpus().length) {
  const scale = load / Math.max(1, cpus);
  return Number.isFinite(scale) ? Math.min(3, Math.max(1, scale)) : 1;
}
export const BASE_WAIT_MS = 30000;

/** Open the real assembled page signed in as `user`. */
export async function openReal(browser, info, user = 'alice', ctxOpts = {}, beforeGoto = null) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, ...ctxOpts });
  const page = await ctx.newPage();
  page.setDefaultTimeout(Math.round(BASE_WAIT_MS * loadScale()));
  const w = watch(page);
  const pending = new Set();
  page.on('request', request => pending.add(request));
  page.on('requestfinished', request => pending.delete(request));
  page.on('requestfailed', request => pending.delete(request));
  const tok = info.tokens[user];
  await page.addInitScript(([t]) => { try { window.sessionStorage.setItem('hcn.tokens', JSON.stringify({ access: t, id: t })); } catch { /* storage blocked */ } }, [tok]);
  if (beforeGoto) await beforeGoto(ctx, page);
  await page.goto(`${info.base}/#/room/${encodeURIComponent(info.room)}`);
  try {
    await page.locator('[data-sub]').first().waitFor({ timeout: 15000 });
  } catch (error) {
    const body = await page.locator('body').innerText().catch(() => 'page unavailable');
    const detail = JSON.stringify({ errors: w.errors, failed: w.failed, http: w.bad, pending: [...pending].map(request => request.url()), url: page.url(), body: body.slice(0, 1200) });
    await ctx.close();
    throw new Error(`Collaboration room failed to open: ${detail}`, { cause: error });
  }
  return { ctx, page, w };
}

export async function openSub(page, sub) {
  await page.locator(`[data-sub="${sub}"]`).click();
  await page.locator(`#hcn-p-${sub}`).waitFor({ state: 'visible' });
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Wait until the page stops changing: no DOM mutation for `quietMs` and two animation frames painted, or `timeoutMs` passed.
 *  A condition wait that replaces fixed sleeps after a click or a fetch: it returns as soon as the page is quiet on a fast host and
 *  keeps waiting on a loaded one, instead of guessing one duration for both. */
export async function settled(page, { quietMs = 120, timeoutMs = 5000 } = {}) {
  await page.evaluate(([quiet, limit]) => new Promise((resolve) => {
    const t0 = performance.now();
    let last = t0;
    const mo = new MutationObserver(() => { last = performance.now(); });
    mo.observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
    const tick = () => {
      const now = performance.now();
      if (now - last >= quiet || now - t0 >= limit) { mo.disconnect(); requestAnimationFrame(() => requestAnimationFrame(() => resolve())); return; }
      setTimeout(tick, Math.min(50, quiet));
    };
    tick();
  }), [quietMs, timeoutMs]);
}
