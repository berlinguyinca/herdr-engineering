// Shared helpers for the T16 UI gates: browser launch, thresholds (ui-gates.yaml), the real integrated server, and
// per-page watchers for console errors, CSP violations and failed requests.
import { spawn } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
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
export const launch = () => chromium.launch({ headless: true, args: ['--no-sandbox'] });

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

/** Open the real assembled page signed in as `user`. */
export async function openReal(browser, info, user = 'alice', ctxOpts = {}, beforeGoto = null) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, ...ctxOpts });
  const page = await ctx.newPage();
  const w = watch(page);
  const tok = info.tokens[user];
  await page.addInitScript(([t]) => { try { window.sessionStorage.setItem('hcn.tokens', JSON.stringify({ access: t, id: t })); } catch { /* storage blocked */ } }, [tok]);
  if (beforeGoto) await beforeGoto(ctx, page);
  await page.goto(`${info.base}/#/room/${encodeURIComponent(info.room)}`);
  await page.locator('[data-sub]').first().waitFor({ timeout: 15000 });
  return { ctx, page, w };
}

export async function openSub(page, sub) {
  await page.locator(`[data-sub="${sub}"]`).click();
  await page.locator(`#hcn-p-${sub}`).waitFor({ state: 'visible' });
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
