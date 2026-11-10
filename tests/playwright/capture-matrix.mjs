#!/usr/bin/env node
// ONE capture entry point for the dashboard: every route in docs/dashboard-sitemap.json x the modes you pick x light/dark.
// The same file runs for a before and an after checkout (copy/overlay it, run it from that checkout), so both sides come out of identical code.
//
//   node tests/playwright/capture-matrix.mjs --out <dir> [--modes phone,ipad-portrait,ipad-landscape,desktop,local] [--appearance light,dark|light|dark|both]
//        [--routes all|slug,slug,missions,analytics*] [--jobs 4] [--local-size WxH[@dpr]] [--list] [--include-skipped]
//
//   defaults: all five modes x light+dark x all routes. --list prints the plan (and every mode's exact viewport) and captures nothing.
//
// Output in --out: <slug>__<viewport-name>__<appearance>.png, map.json (the `sw review import --mapping` file), coverage.json (one row per
// capture: ok | degraded (page rendered but asked for an API route without a fixture, or showed an error state) | failed | skipped + why).
// Screenwright pairing: modes use the Screenwright web matrix sizes and device_class / orientation names. screen_id is the slug, except the two
// tablet orientations, whose ids get "-portrait" / "-landscape" so the importer never sees a duplicate (device_class tablet, same slug).
// `local` is the host's own main display (logical size and devicePixelRatio, queried on macOS; elsewhere pass --local-size), captured under
// device_class "local" so it never collides with a Screenwright class.
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { launch } from './ui-gates-lib.mjs';
import { capture } from './visual-baselines.mjs';
import { allSurfaces } from './dashboard-surfaces.mjs';

// the Screenwright web matrix (packages/web/src/config.ts and core/src/run-options.ts)
export const MODES = {
  phone: { viewport: 'phone-portrait', width: 390, height: 844, device_class: 'phone', orientation: 'portrait', dpr: 1, idSuffix: '' },
  'ipad-portrait': { viewport: 'tablet-portrait', width: 820, height: 1180, device_class: 'tablet', orientation: 'portrait', dpr: 1, idSuffix: '-portrait' },
  'ipad-landscape': { viewport: 'tablet-landscape', width: 1180, height: 820, device_class: 'tablet', orientation: 'landscape', dpr: 1, idSuffix: '-landscape' },
  desktop: { viewport: 'desktop-landscape', width: 1440, height: 900, device_class: 'desktop', orientation: 'landscape', dpr: 1, idSuffix: '' },
};
export const ALL_MODES = [...Object.keys(MODES), 'local'];

/** The host's main display: logical (CSS) size and devicePixelRatio. macOS reads system_profiler; elsewhere --local-size is required. */
export function localDisplay(override = '') {
  let w; let h; let dpr = 1;
  const m = /^(\d+)x(\d+)(?:@(\d+(?:\.\d+)?))?$/.exec(override);
  if (override && !m) throw new Error(`--local-size must look like 1512x982 or 1512x982@2, got "${override}"`);
  if (m) { w = +m[1]; h = +m[2]; dpr = m[3] ? +m[3] : 1; }
  else if (process.platform === 'darwin') {
    const data = JSON.parse(execFileSync('system_profiler', ['SPDisplaysDataType', '-json'], { encoding: 'utf8' }));
    const displays = data.SPDisplaysDataType.flatMap((g) => g.spdisplays_ndrvs || []);
    const main = displays.find((d) => d.spdisplays_main === 'spdisplays_yes') || displays[0];
    if (!main) throw new Error('no display found; pass --local-size WxH[@dpr]');
    [w, h] = /(\d+) x (\d+)/.exec(main._spdisplays_resolution).slice(1).map(Number);
    const px = /(\d+) x (\d+)/.exec(main._spdisplays_pixels || '');
    dpr = px ? Math.round((+px[1] / w) * 100) / 100 : 1;
  } else throw new Error('the host screen can only be queried on macOS here; pass --local-size WxH[@dpr]');
  return { viewport: `local-${w}x${h}${dpr !== 1 ? `-${dpr}x` : ''}`, width: w, height: h, device_class: 'local', orientation: w >= h ? 'landscape' : 'portrait', dpr, idSuffix: '' };
}

/** Pure and deterministic: the Screenwright metadata of one capture. */
export const screenMeta = (slug, mode) => ({ screen_id: slug + mode.idSuffix, orientation: mode.orientation, device_class: mode.device_class });
export const fileName = (slug, mode, appearance) => `${slug}__${mode.viewport}__${appearance}`;

function parseArgs(argv) {
  const a = { out: '', modes: ALL_MODES, appearance: ['light', 'dark'], routes: ['all'], jobs: 4, localSize: '', list: false, includeSkipped: false };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i]; const v = () => { const x = argv[++i]; if (x === undefined) throw new Error(`${k} needs a value`); return x; };
    if (k === '--out') a.out = path.resolve(v());
    else if (k === '--modes') a.modes = v().split(',').map((s) => s.trim()).filter(Boolean);
    else if (k === '--appearance') { const x = v(); a.appearance = x === 'both' ? ['light', 'dark'] : x.split(',').map((s) => s.trim()); }
    else if (k === '--routes') a.routes = v().split(',').map((s) => s.trim()).filter(Boolean);
    else if (k === '--jobs') a.jobs = Math.max(1, Number(v()) || 1);
    else if (k === '--local-size') a.localSize = v();
    else if (k === '--list') a.list = true;
    else if (k === '--include-skipped') a.includeSkipped = true;
    else throw new Error(`unknown flag ${k}`);
  }
  const badMode = a.modes.filter((m) => !ALL_MODES.includes(m)); if (badMode.length) throw new Error(`unknown mode(s) ${badMode}; choose from ${ALL_MODES}`);
  const badApp = a.appearance.filter((m) => !['light', 'dark'].includes(m)); if (badApp.length || !a.appearance.length) throw new Error('--appearance takes light, dark, light,dark or both');
  if (!a.list && !a.out) throw new Error('--out <dir> is required');
  return a;
}

/** `all`, an exact slug, a slug without its "dashboard-" prefix, a route pattern, or any of those ending in * (prefix match). */
export function selectSurfaces(all, tokens) {
  if (tokens.includes('all')) return all;
  const short = (s) => s.slug.replace(/^dashboard-/, '');
  const hit = (tok) => all.filter((s) => (tok.endsWith('*') ? [s.slug, short(s), s.route].some((x) => x.startsWith(tok.slice(0, -1))) : [s.slug, short(s), s.route].includes(tok)));
  const out = new Set(); const unknown = [];
  for (const t of tokens) { const h = hit(t); if (!h.length) unknown.push(t); h.forEach((s) => out.add(s)); }
  if (unknown.length) throw new Error(`no route matches ${unknown.join(', ')}; known slugs: ${all.map((s) => short(s)).join(', ')}`);
  return all.filter((s) => out.has(s));
}

async function main() {
  const a = parseArgs(process.argv.slice(2));
  const modes = a.modes.map((m) => (m === 'local' ? localDisplay(a.localSize) : MODES[m]));
  const surfaces = selectSurfaces(allSurfaces(), a.routes);
  const jobs = [];
  for (const s of surfaces) for (const mode of modes) for (const appearance of a.appearance) jobs.push({ s, mode, appearance });
  console.log(`plan: ${surfaces.length} routes/surfaces x ${modes.length} modes x ${a.appearance.length} appearances = ${jobs.length} captures`);
  for (const m of modes) console.log(`  mode ${m.viewport}: ${m.width}x${m.height} @${m.dpr}x device_class=${m.device_class} orientation=${m.orientation}`);
  if (a.list) { for (const s of surfaces) console.log(`  ${s.slug}  ${s.route}  #${s.hash}  fixture=${s.fixtureStatus}`); return; }
  fs.mkdirSync(a.out, { recursive: true });
  const browser = await launch();
  const rows = []; let next = 0;
  const worker = async () => {
    while (next < jobs.length) {
      const { s, mode, appearance } = jobs[next++];
      const name = fileName(s.slug, mode, appearance);
      const row = { name, slug: s.slug, route: s.route, hash: s.hash, mode: mode.viewport, appearance, ...screenMeta(s.slug, mode), width: mode.width, height: mode.height, dpr: mode.dpr };
      if (s.fixtureStatus === 'skipped' && !a.includeSkipped) { rows.push({ ...row, status: 'skipped', reason: s.skipReason }); console.log('skipped', name, '-', s.skipReason); continue; }
      try {
        const apiLog = new Set();   // per capture: jobs run in parallel
        let probe = null;
        const wrapped = { ...s, apiLog, steps: async (page) => { await s.steps(page); probe = await page.evaluate(() => ({ error: !!document.querySelector('#view herdr-error, #view [data-error]'), text: (document.querySelector('#view')?.innerText || '').slice(0, 4000) })); } };
        const r = await capture(browser, null, wrapped, mode.viewport, appearance, { regions: false, size: { width: mode.width, height: mode.height }, dpr: mode.dpr });
        fs.writeFileSync(path.join(a.out, name + '.png'), r.page);
        const misses = [...apiLog].filter((l) => l.endsWith('MISS')).map((l) => l.replace(' -> MISS', ''));
        const errorState = probe?.error || /no fixture for/.test(probe?.text || '');
        rows.push({ ...row, status: misses.length || errorState ? 'degraded' : 'ok', ...(misses.length ? { misses: [...new Set(misses)].sort() } : {}), ...(errorState ? { errorState: true } : {}) });
        console.log(misses.length || errorState ? 'degraded' : 'ok', name);
      } catch (e) { rows.push({ ...row, status: 'failed', reason: String(e.message).split('\n')[0].slice(0, 300) }); console.log('FAIL', name, String(e.message).split('\n')[0].slice(0, 200)); }
    }
  };
  await Promise.all(Array.from({ length: Math.min(a.jobs, jobs.length) }, worker));
  await browser.close();
  rows.sort((x, y) => x.name.localeCompare(y.name));
  const shots = rows.filter((r) => r.status === 'ok' || r.status === 'degraded');
  fs.writeFileSync(path.join(a.out, 'map.json'), JSON.stringify(shots.map((r) => ({ screen_id: r.screen_id, before: r.name, after: r.name, orientation: r.orientation, device_class: r.device_class, appearance: r.appearance })), null, 1));
  fs.writeFileSync(path.join(a.out, 'coverage.json'), JSON.stringify(rows, null, 1));
  const count = (st) => rows.filter((r) => r.status === st).length;
  console.log(`done: ok ${count('ok')}, degraded ${count('degraded')}, failed ${count('failed')}, skipped ${count('skipped')} of ${rows.length}`);
  process.exitCode = count('failed') ? 1 : 0;
}
if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(import.meta.filename)) main().catch((e) => { console.error(e.message); process.exit(2); });
