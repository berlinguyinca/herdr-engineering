#!/usr/bin/env node
// Browser verification harness for artifact share links (docs/superpowers/specs/2026-10-30-artifact-share-links-design.md).
//
//   node scripts/share-browser-check.mjs --browser chromium|firefox|webkit [--out DIR]
//
// Starts scripts/share_browser_server.py (the real make_share_server over temp bundles from tests/share-fixtures, TLS
// with a throw-away certificate, a fake Cognito hosted UI) and drives one Playwright browser through: HTML renders,
// the parquet table appears from range requests (observed server-side, including a suffix range and its preflight),
// the share cannot read the host origin's sessionStorage (SecurityError), an external CDN URL is blocked by the CSP,
// the PDF headers and display, revoked links show the neutral 404, and lab sign-in (allowed vs unlisted user, then
// revoke kills the minted session). Prints a pass/fail table, writes results.json + screenshots to --out, and exits
// non-zero when any assertion fails. Every assertion runs even when an earlier one fails.
//
// The duckdb-wasm binaries (~40 MB) are not in git: on first use the pinned npm tarball and the pinned parquet
// extension are downloaded into scripts/.share-browser-cache/ (gitignored), checked against the pinned sha512/sha256,
// and copied into a temp copy of the fixtures for each run.
import { execFileSync, spawn } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import * as playwright from 'playwright';

const REPO = path.resolve(import.meta.dirname, '..');
const FIXTURES = path.join(REPO, 'tests', 'share-fixtures');
const NOT_FOUND = 'Not found\n';
const PAGE_TIMEOUT = 90_000;
const CACHE = path.join(REPO, 'scripts', '.share-browser-cache');
// Must match tests/share-fixtures/_build/build-vendor.sh (vendor/duckdb.mjs is built from the same version).
const DUCKDB_TARBALL = {
  url: 'https://registry.npmjs.org/@duckdb/duckdb-wasm/-/duckdb-wasm-1.33.1-dev57.0.tgz',
  integrity: 'sha512-ndn5l2EHq1PEMvCg8G6lRV0vjhSUcbqdM/niNBTTqH4/PyyUUVKbge0uUyCnj27b9SF/CiqBwDsgKLTqBvaxdw==',
  files: ['duckdb-browser-eh.worker.js', 'duckdb-eh.wasm'],
};
const DUCKDB_PARQUET_EXTENSION = {
  url: 'https://extensions.duckdb.org/v1.5.4/wasm_eh/parquet.duckdb_extension.wasm',
  sha256: '4845705bbd69fc9ad52878d96a505c73cae4a6c509822079cc2413e5eb437f95',
  dest: 'extensions/v1.5.4/wasm_eh/parquet.duckdb_extension.wasm',
};

async function download(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`download ${url}: ${r.status}`);
  return Buffer.from(await r.arrayBuffer());
}

// The pinned duckdb-wasm files, from the cache or freshly downloaded and verified. Returns {name: absolute path}.
async function duckdbAssets() {
  const dir = path.join(CACHE, path.basename(DUCKDB_TARBALL.url, '.tgz'));
  const out = Object.fromEntries(DUCKDB_TARBALL.files.map((f) => [f, path.join(dir, f)]));
  if (!DUCKDB_TARBALL.files.every((f) => fs.existsSync(out[f]))) {
    const tgz = await download(DUCKDB_TARBALL.url);
    const [algo, want] = DUCKDB_TARBALL.integrity.split('-');
    const got = crypto.createHash(algo).update(tgz).digest('base64');
    if (got !== want) throw new Error(`duckdb-wasm tarball integrity mismatch: ${algo}-${got}`);
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'duckdb-wasm-'));
    try {
      fs.writeFileSync(path.join(tmp, 'pkg.tgz'), tgz);
      execFileSync('tar', ['-xzf', path.join(tmp, 'pkg.tgz'), '-C', tmp,
        ...DUCKDB_TARBALL.files.map((f) => `package/dist/${f}`)]);
      fs.mkdirSync(dir, { recursive: true });
      for (const f of DUCKDB_TARBALL.files) fs.copyFileSync(path.join(tmp, 'package', 'dist', f), out[f]);
    } finally {
      fs.rmSync(tmp, { recursive: true, force: true });
    }
  }
  const ext = path.join(CACHE, 'duckdb-extensions', DUCKDB_PARQUET_EXTENSION.dest);
  const sha = (file) => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  if (!fs.existsSync(ext) || sha(ext) !== DUCKDB_PARQUET_EXTENSION.sha256) {
    const body = await download(DUCKDB_PARQUET_EXTENSION.url);
    const got = crypto.createHash('sha256').update(body).digest('hex');
    if (got !== DUCKDB_PARQUET_EXTENSION.sha256) throw new Error(`parquet extension sha256 mismatch: ${got}`);
    fs.mkdirSync(path.dirname(ext), { recursive: true });
    fs.writeFileSync(ext, body);
  }
  out[DUCKDB_PARQUET_EXTENSION.dest] = ext;
  return out;
}

// A temp copy of tests/share-fixtures with the cached duckdb-wasm files placed in the duckdb bundle's vendor/.
async function assembleFixtures() {
  const assets = await duckdbAssets();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'share-fixtures-'));
  fs.cpSync(FIXTURES, dir, { recursive: true });
  const vendor = path.join(dir, 'html-parquet-duckdb', 'vendor');
  for (const [name, src] of Object.entries(assets)) {
    fs.mkdirSync(path.dirname(path.join(vendor, name)), { recursive: true });
    fs.copyFileSync(src, path.join(vendor, name));
  }
  return dir;
}

function parseArgs(argv) {
  const out = { browser: null, out: null };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--browser') out.browser = argv[++i];
    else if (argv[i] === '--out') out.out = argv[++i];
    else throw new Error(`unknown argument ${argv[i]}`);
  }
  if (!['chromium', 'firefox', 'webkit'].includes(out.browser)) {
    throw new Error('usage: share-browser-check.mjs --browser chromium|firefox|webkit [--out DIR]');
  }
  out.out ||= fs.mkdtempSync(path.join(os.tmpdir(), `share-browser-check-${out.browser}-`));
  fs.mkdirSync(out.out, { recursive: true });
  return out;
}

function python() {
  if (process.env.PYTHON) return process.env.PYTHON;
  const venv = path.join(REPO, '.venv', 'bin', 'python');
  return fs.existsSync(venv) ? venv : 'python3';
}

async function startLauncher(fixtures) {
  const proc = spawn(python(), ['-m', 'scripts.share_browser_server', '--fixtures', fixtures], {
    cwd: REPO, stdio: ['pipe', 'pipe', 'pipe'],
  });
  proc.stderr.on('data', (d) => process.stderr.write(`[share-server] ${d}`));
  const info = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('share server start timeout')), 60_000);
    readline.createInterface({ input: proc.stdout }).once('line', (line) => {
      clearTimeout(timer);
      try { resolve(JSON.parse(line)); } catch (e) { reject(e); }
    });
    proc.once('exit', (code) => { clearTimeout(timer); reject(new Error(`share server exited (${code})`)); });
  });
  return { proc, info };
}

// Thrown when the environment cannot show the property at all (reported as NOT VERIFIED, never as a pass).
class NotVerified extends Error {}

function assert(cond, message) {
  if (!cond) throw new Error(message);
}

async function control(info, method, route) {
  const r = await fetch(info.control + route, { method });
  assert(r.ok, `control ${route}: ${r.status}`);
  return r.json();
}

// Watch one page: console lines, page errors, crashes, and every response / failed request (a CSP-blocked request
// may still be announced as a request; what matters is that no response came back).
function watch(page) {
  const seen = { console: [], errors: [], responses: [], failed: [] };
  page.on('console', (m) => seen.console.push(`${m.type()}: ${m.text()}`.slice(0, 500)));
  page.on('pageerror', (e) => seen.errors.push(String(e).slice(0, 500)));
  page.on('crash', () => seen.errors.push('renderer crashed'));
  page.on('response', (r) => seen.responses.push(r.url()));
  page.on('requestfailed', (r) => seen.failed.push(`${r.url()} ${r.failure() && r.failure().errorText}`));
  return seen;
}

function diagnostics(seen) {
  return [...seen.errors.map((e) => `pageerror: ${e}`), ...seen.console.slice(-12)].join('\n');
}

async function waitForStatus(page) {
  await page.waitForFunction(() => {
    const el = document.getElementById('status');
    return el && el.dataset.state;
  }, null, { timeout: PAGE_TIMEOUT });
  return page.evaluate(() => ({
    state: document.getElementById('status').dataset.state,
    text: document.getElementById('status').textContent.slice(0, 2000),
    rows: document.querySelectorAll('#data tbody tr').length,
    probe: window.__probe,
  }));
}

function parquetLog(log, since) {
  return log.filter((e) => e.at >= since && e.path.endsWith('/data/table.parquet'));
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const results = [];
  const check = async (name, fn) => {
    try {
      const detail = await fn();
      results.push({ name, ok: true, detail: detail ?? '' });
    } catch (e) {
      results.push({ name, ok: e instanceof NotVerified ? null : false, detail: String(e && e.message || e) });
    }
  };
  const fixtures = await assembleFixtures();
  const { proc, info } = await startLauncher(fixtures);
  const shareUrl = (id) => `${info.origin}/s/${info.tokens[id]}/`;
  const launchOptions = { headless: true };
  // The default headless shell has no PDF viewer; the full Chromium build in new headless mode does.
  if (args.browser === 'chromium') launchOptions.channel = 'chromium';
  if (args.browser === 'firefox') launchOptions.firefoxUserPrefs = { 'pdfjs.disabled': false };
  const browser = await playwright[args.browser].launch(launchOptions);
  const context = await browser.newContext({ ignoreHTTPSErrors: true, acceptDownloads: true,
    viewport: { width: 1100, height: 800 } });
  const shot = (page, name) => page.screenshot({ path: path.join(args.out, `${name}.png`), fullPage: false });
  try {
    // ---------------------------------------------------------------- the two parquet bundles (public)
    for (const [id, label] of [['pub-hyparquet', 'hyparquet'], ['pub-duckdb', 'duckdb']]) {
      const page = await context.newPage();
      const seen = watch(page);
      let status = null;
      let planted = null;
      const since = Date.now() / 1000;
      await check(`${label}: plant a sessionStorage sentinel on the host origin`, async () => {
        await page.goto(`${info.origin}/s/callback`);
        planted = await page.evaluate(() => {
          sessionStorage.setItem('herdr-browser-check-sentinel', 'planted');
          return sessionStorage.getItem('herdr-browser-check-sentinel');
        });
        assert(planted === 'planted', `sentinel not stored: ${planted}`);
        return `host origin ${info.origin} holds the sentinel`;
      });
      await check(`${label}: HTML renders`, async () => {
        const response = await page.goto(shareUrl(id), { timeout: PAGE_TIMEOUT });
        assert(response && response.status() === 200, `entry status ${response && response.status()}`);
        status = await waitForStatus(page);
        await shot(page, `${label}`);
        const h1 = await page.locator('h1').textContent();
        assert(/^Parquet via/.test(h1), `heading ${h1}`);
        return `h1="${h1}", origin seen by the page: ${status.probe && status.probe.origin}`;
      });
      await check(`${label}: parquet table appears`, async () => {
        assert(status, 'page did not finish');
        assert(status.state === 'ok', `page error: ${status.text}\n${diagnostics(seen)}`);
        assert(status.rows === 20, `rows rendered: ${status.rows}`);
        assert(status.probe.rowsTotal === 40000, `rowsTotal ${status.probe.rowsTotal}`);
        return `20 rows rendered of ${status.probe.rowsTotal}`;
      });
      await check(`${label}: range requests seen by the server (incl. suffix + preflight)`, async () => {
        const log = parquetLog(await control(info, 'GET', '/log'), since);
        const bounded = log.filter((e) => e.method === 'GET' && e.status === 206 && /^bytes=\d+-\d*$/.test(e.range || ''));
        const suffix = log.filter((e) => e.method === 'GET' && e.status === 206 && /^bytes=-\d+$/.test(e.range || ''));
        const preflight = log.filter((e) => e.method === 'OPTIONS' && e.status === 204);
        const whole = log.filter((e) => e.method === 'GET' && e.status === 200);
        const summary = log.map((e) => `${e.method} ${e.status} ${e.range || '-'}`).join('; ');
        assert(bounded.length > 0, `no bounded 206 range read: ${summary}`);
        assert(suffix.length > 0, `no suffix 206 range read: ${summary}`);
        assert(preflight.length > 0, `no OPTIONS preflight answered 204: ${summary}`);
        assert(whole.length === 0, `the whole file was fetched with a 200 GET: ${summary}`);
        assert(status && status.probe.suffix && status.probe.suffix.status === 206
          && status.probe.suffix.magic === 'PAR1', `suffix read in page: ${JSON.stringify(status && status.probe.suffix)}`);
        return summary;
      });
      await check(`${label}: share script cannot read host sessionStorage (SecurityError)`, async () => {
        assert(planted === 'planted', 'sentinel was not planted, so this proves nothing');
        assert(status && status.probe, 'no probe result');
        assert(status.probe.sessionStorage === 'threw:SecurityError', `sessionStorage: ${status.probe.sessionStorage}`);
        return `sessionStorage ${status.probe.sessionStorage}; localStorage ${status.probe.localStorage}`;
      });
      await check(`${label}: external CDN URL blocked by CSP`, async () => {
        assert(status && status.probe, 'no probe result');
        const blocked = status.probe.csp.filter((v) => String(v.blockedURI).startsWith('https://cdn.jsdelivr.net'));
        assert(blocked.length > 0, `no securitypolicyviolation for the CDN: ${JSON.stringify(status.probe.csp)}`);
        const answered = seen.responses.filter((u) => u.startsWith('https://cdn.jsdelivr.net'));
        assert(answered.length === 0, `the CDN answered: ${answered.join(', ')}`);
        const failed = seen.failed.filter((u) => u.startsWith('https://cdn.jsdelivr.net'));
        return `violation ${blocked[0].directive} for ${blocked[0].blockedURI}; no response`
          + (failed.length ? `; request failed: ${failed[0].split(' ').slice(1).join(' ')}` : '');
      });
      await page.close();
    }

    // ---------------------------------------------------------------- PDF
    const pdfEntry = `${shareUrl('pub-pdf')}f/report.pdf`;
    await check('pdf: served inline as application/pdf with the PDF CSP', async () => {
      const r = await context.request.get(pdfEntry);
      const h = r.headers();
      assert(r.status() === 200, `status ${r.status()}`);
      assert(h['content-type'] === 'application/pdf', `content-type ${h['content-type']}`);
      assert(h['content-disposition'] === 'inline', `content-disposition ${h['content-disposition']}`);
      assert(h['content-security-policy'] === "frame-ancestors 'none'", `csp ${h['content-security-policy']}`);
      assert(h['x-content-type-options'] === 'nosniff', 'nosniff');
      return 'headers OK';
    });
    await check('pdf: displays in the browser (screenshot)', async () => {
      const page = await context.newPage();
      let download = null;
      page.on('download', (d) => { download = d; });
      let navError = null;
      try {
        await page.goto(shareUrl('pub-pdf'), { timeout: 30_000 });
      } catch (e) {
        navError = String(e.message || e).split('\n')[0];
      }
      await page.waitForTimeout(3000);
      await shot(page, 'pdf').catch(() => {});
      await page.close();
      if (download && args.browser === 'webkit') {
        // Playwright's Linux WebKit build has no PDF plug-in and always downloads; Safari's viewer is not testable here.
        throw new NotVerified('Playwright WebKit (Linux) downloads every PDF: display not verifiable here');
      }
      assert(!download, `the browser downloaded the PDF instead of displaying it (${navError || 'no navigation error'})`);
      assert(!navError, `navigation failed: ${navError}`);
      return `displayed; screenshot ${path.join(args.out, 'pdf.png')}`;
    });

    // ---------------------------------------------------------------- revoke (public)
    await check('revoke (public): the next request shows the neutral 404 page', async () => {
      const page = await context.newPage();
      const before = await page.goto(shareUrl('pub-revoke'));
      assert(before.status() === 200, `before revoke: ${before.status()}`);
      await control(info, 'POST', '/revoke?share=pub-revoke');
      const after = await page.goto(shareUrl('pub-revoke'));
      const body = await after.text();
      await shot(page, 'revoked-public');
      await page.close();
      assert(after.status() === 404 && body === NOT_FOUND, `after revoke: ${after.status()} ${JSON.stringify(body)}`);
      const file = await context.request.get(`${shareUrl('pub-revoke')}f/data/table.parquet`);
      assert(file.status() === 404 && (await file.text()) === NOT_FOUND, `file after revoke: ${file.status()}`);
      return 'entry and files are the uniform 404';
    });

    // ---------------------------------------------------------------- lab: allowed user, unlisted user, revoke
    let sessionEntry = null;
    await check('lab: allowed email signs in (PKCE via fake Cognito) and the bundle loads', async () => {
      await control(info, 'POST', '/user?name=alice');
      const lab = await browser.newContext({ ignoreHTTPSErrors: true });
      const page = await lab.newPage();
      const seen = watch(page);
      const since = Date.now() / 1000;
      try {
        await page.goto(shareUrl('lab-hyparquet'));
        await page.waitForURL(/\/k\/[^/]+\/f\/index\.html$/, { timeout: PAGE_TIMEOUT });
        const status = await waitForStatus(page);
        await shot(page, 'lab-alice');
        sessionEntry = page.url();
        const log = (await control(info, 'GET', '/log')).filter((e) => e.at >= since);
        const session = log.filter((e) => e.method === 'POST' && e.path === '/s/<token>/session');
        assert(session.length === 1 && session[0].status === 200, `session POST: ${JSON.stringify(session)}`);
        assert(status.state === 'ok' && status.rows === 20, `page: ${status.state} ${status.text}\n${diagnostics(seen)}`);
        const ranged = parquetLog(log, since).filter((e) => e.path.includes('/k/<session>/f/') && e.status === 206);
        assert(ranged.length > 0, 'no range reads under the session path');
        assert(status.probe.sessionStorage === 'threw:SecurityError', `sessionStorage ${status.probe.sessionStorage}`);
        return `session minted; ${ranged.length} range reads under /k/<session>/f/`;
      } finally {
        await lab.close();
      }
    });
    await check('lab: user not on the allow-list is refused', async () => {
      await control(info, 'POST', '/user?name=eve');
      const lab = await browser.newContext({ ignoreHTTPSErrors: true });
      const page = await lab.newPage();
      const since = Date.now() / 1000;
      try {
        await page.goto(shareUrl('lab-hyparquet'));
        await page.waitForURL(`${info.origin}/s/callback`, { timeout: PAGE_TIMEOUT });
        await page.waitForSelector('#status.failed', { timeout: PAGE_TIMEOUT });
        const text = await page.locator('#status').textContent();
        await shot(page, 'lab-eve');
        const session = (await control(info, 'GET', '/log'))
          .filter((e) => e.at >= since && e.method === 'POST' && e.path === '/s/<token>/session');
        assert(text === 'This link isn’t available.', `status text ${text}`);
        assert(session.length === 1 && session[0].status === 404, `session POST: ${JSON.stringify(session)}`);
        assert(!/\/k\//.test(page.url()), `navigated to ${page.url()}`);
        return 'neutral message; session POST was the 404';
      } finally {
        await control(info, 'POST', '/user?name=alice');
        await lab.close();
      }
    });
    await check('revoke (lab): the minted session URL and the link become the neutral 404', async () => {
      assert(sessionEntry, 'no session URL from the sign-in check');
      const page = await context.newPage();
      const before = await page.goto(sessionEntry);
      assert(before.status() === 200, `before revoke: ${before.status()}`);
      await control(info, 'POST', '/revoke?share=lab-hyparquet');
      const after = await page.goto(sessionEntry);
      const body = await after.text();
      const link = await context.request.get(shareUrl('lab-hyparquet'));
      await page.close();
      assert(after.status() === 404 && body === NOT_FOUND, `session URL after revoke: ${after.status()}`);
      assert(link.status() === 404 && (await link.text()) === NOT_FOUND, `link after revoke: ${link.status()}`);
      return 'session URL and link are the uniform 404';
    });
  } finally {
    await context.close().catch(() => {});
    await browser.close().catch(() => {});
    proc.stdin.end();
    proc.kill('SIGTERM');
    await new Promise((resolve) => (proc.exitCode !== null ? resolve() : proc.once('exit', resolve)));
    fs.rmSync(fixtures, { recursive: true, force: true });
  }

  const version = browser.version();
  fs.writeFileSync(path.join(args.out, 'results.json'),
    JSON.stringify({ browser: args.browser, version, results }, null, 2));
  console.log(`\n${args.browser} ${version}`);
  for (const r of results) {
    console.log(`${r.ok === null ? 'N/V ' : r.ok ? 'PASS' : 'FAIL'}  ${r.name}`);
    if (r.detail) console.log(`      ${String(r.detail).split('\n').join('\n      ')}`);
  }
  console.log(`\nresults and screenshots: ${args.out}`);
  return results.every((r) => r.ok !== false) ? 0 : 1;
}

main().then((code) => process.exit(code), (e) => { console.error(e); process.exit(2); });
