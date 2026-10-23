// Screenshot baselines and visual-regression compare for the collaboration page (T16 UI gates).
//
// Conventions follow autospec (gen-screenshots.mjs / ux-ui-workstream.md / qa-visual-findings.sh):
//   * files:   docs/assets/screenshots/<surface-slug>__<viewport>[-dark].png   (viewports: desktop 1280x800, tablet 768x1024, mobile 375x667)
//   * budget:  a visual diff above 0.1% of pixels fails (ui-gates.yaml screenshots.max_diff_pct); light AND dark are both required.
//   * the page is made deterministic first: a fixed server clock, a fixed browser clock, reduced motion, animations disabled,
//     the caret hidden, and server-generated ids rewritten to stable tokens (they are random per run).
//
// Commands (no environment variables; arguments only):
//   node tests/playwright/visual-baselines.mjs update          # (re)write every baseline PNG from a fresh deterministic run
//   node tests/playwright/visual-baselines.mjs check           # compare every surface with its baseline; exit 1 on any miss
//   node tests/playwright/visual-baselines.mjs update lineage  # only surfaces whose slug contains "lineage"
// Baselines are tied to this Chromium build and this host's fonts: regenerate them with `update` on the CI image and review the diff.
import { mkdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import path from 'node:path';
import { CFG, REPO, launch, openReal, openSub, startRealServer } from './ui-gates-lib.mjs';

export const BASELINE_DIR = path.join(REPO, 'docs', 'assets', 'screenshots');
export const REPORT_DIR = path.join(REPO, '.autospec', 'reports', 'visual');
export const VIEWPORTS = ['desktop', 'tablet', 'mobile'];
const MAX_HEIGHT = 2400;     // taller pages are clipped so a long list cannot make a baseline huge

/** The surfaces under test: slug, how to reach it, a selector that proves it rendered, and optional extra steps. */
export const SURFACES = [
  { slug: 'collab-rooms', hash: '/rooms', proof: '#hcn-rooms' },
  { slug: 'collab-chat', sub: 'chat', proof: '#hcn-log' },
  { slug: 'collab-tabs', sub: 'tabs', proof: '#hcn-tabs [role=tab]' },
  { slug: 'collab-lineage', sub: 'lineage', proof: 'svg.lin-svg g[data-node]', steps: async (page) => { await page.locator('g[data-node="attempt:a1"]').focus(); await page.keyboard.press('Enter'); await page.locator('.wb-comment').first().waitFor(); await page.locator('.wb-steer').first().waitFor(); await page.evaluate(() => document.activeElement?.blur()); } },
  { slug: 'collab-lineage-gantt', sub: 'lineage', proof: 'svg.lin-svg', steps: async (page) => { await page.locator('[data-view="gantt"]').click(); await page.locator('svg.lin-svg[role=img]').waitFor(); } },
  { slug: 'collab-attention', sub: 'attention', proof: '.cx-attn-item' },
  { slug: 'collab-progress', sub: 'progress', proof: '#hcn-p-progress' },
  { slug: 'collab-links', sub: 'links', proof: '#hcn-links li' },
  { slug: 'collab-proposals', sub: 'proposals', proof: '[data-proposal]', steps: async (page) => { await page.locator('[data-proposal]').first().click(); await page.locator('#hcn-review [data-decide]').first().waitFor(); } },
];

export const fileFor = (slug, viewport, scheme) => path.join(BASELINE_DIR, `${slug}__${viewport}${scheme === 'dark' ? '-dark' : ''}.png`);

// Server-generated ids are random per run; rewrite them to stable tokens so the pixels are comparable.
const NORMALIZE = () => {
  const rules = [[/\b(collab|prp|agent|room|msg|task|dlg|lnk|link|evt)_[0-9a-z]{16,}\b/g, '$1_ID'], [/(@?)\bext-[a-z0-9-]*?-[a-z0-9]{4}\b/g, '$1ext-agent-xxxx'],
    [/\bkey:[0-9a-f]{12}\b/g, 'key:KEYID'], [/\b[0-9a-f]{16,64}\b/g, (m) => 'HEX'.padEnd(Math.min(m.length, 12), 'x')], [/\bik-[0-9a-f]+\b/g, 'ik-KEY']];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n; while ((n = walker.nextNode())) { let v = n.nodeValue; for (const [re, to] of rules) v = v.replace(re, to); if (v !== n.nodeValue) n.nodeValue = v; }
};

export async function newDeterministicServer() {
  return startRealServer({ fixed_clock: true });
}

/** Capture one surface at one viewport / colour scheme. Returns a PNG Buffer. */
export async function capture(browser, info, surface, viewport, scheme) {
  const { ctx, page } = await openReal(browser, info, 'alice', { viewport: CFG.viewports[viewport], colorScheme: scheme, reducedMotion: 'reduce', deviceScaleFactor: 1 });
  try {
    await page.clock.setFixedTime(new Date(CFG.screenshots.fixed_clock));
    if (surface.hash) { await page.evaluate((h) => { window.location.hash = h; }, surface.hash); } else { await openSub(page, surface.sub); }
    await page.locator(surface.proof).first().waitFor({ timeout: 15000 });
    if (surface.steps) await surface.steps(page);
    await page.addStyleTag({ content: '*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important;scroll-behavior:auto!important}' });
    await page.evaluate(() => document.fonts.ready);
    await page.waitForTimeout(600);
    await page.evaluate(NORMALIZE);
    const height = Math.min(MAX_HEIGHT, await page.evaluate(() => Math.max(document.documentElement.scrollHeight, innerHeight)));
    return await page.screenshot({ clip: { x: 0, y: 0, width: CFG.viewports[viewport].width, height }, animations: 'disabled', caret: 'hide' });
  } finally { await ctx.close(); }
}

/** Compare two PNGs in a blank page (the canvas is the PNG decoder: this repo takes no image library). */
export async function diffPng(browser, actual, expected, tol = CFG.screenshots.channel_tolerance) {
  const page = await browser.newPage();
  try {
    const out = await page.evaluate(async ([a, b, t]) => {
      const load = async (b64) => { const blob = await (await fetch(`data:image/png;base64,${b64}`)).blob(); const bmp = await createImageBitmap(blob); const c = new OffscreenCanvas(bmp.width, bmp.height); const g = c.getContext('2d'); g.drawImage(bmp, 0, 0); return { w: bmp.width, h: bmp.height, d: g.getImageData(0, 0, bmp.width, bmp.height).data }; };
      const A = await load(a); const B = await load(b);
      if (A.w !== B.w || A.h !== B.h) return { sizeMismatch: true, actual: [A.w, A.h], expected: [B.w, B.h], pct: 100, diffPixels: -1 };
      let bad = 0; const mark = new Uint8ClampedArray(A.d.length);
      for (let i = 0; i < A.d.length; i += 4) {
        const dr = Math.abs(A.d[i] - B.d[i]); const dg = Math.abs(A.d[i + 1] - B.d[i + 1]); const db = Math.abs(A.d[i + 2] - B.d[i + 2]);
        const differs = dr > t || dg > t || db > t;
        if (differs) { bad += 1; mark[i] = 255; mark[i + 3] = 255; } else { mark[i] = A.d[i] >> 1; mark[i + 1] = A.d[i + 1] >> 1; mark[i + 2] = A.d[i + 2] >> 1; mark[i + 3] = 255; }
      }
      const c = new OffscreenCanvas(A.w, A.h); c.getContext('2d').putImageData(new ImageData(mark, A.w, A.h), 0, 0);
      const blob = await c.convertToBlob({ type: 'image/png' });
      const buf = new Uint8Array(await blob.arrayBuffer()); let s = ''; for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode(...buf.subarray(i, i + 0x8000));
      return { sizeMismatch: false, diffPixels: bad, pct: (100 * bad) / (A.w * A.h), diff: btoa(s) };
    }, [actual.toString('base64'), expected.toString('base64'), tol]);
    return out;
  } finally { await page.close(); }
}

/** Check one capture against its baseline. Writes actual + diff PNGs under .autospec/reports/visual on failure. */
export async function checkAgainstBaseline(browser, png, slug, viewport, scheme) {
  const file = fileFor(slug, viewport, scheme);
  const name = path.basename(file, '.png');
  if (!existsSync(file)) return { name, ok: false, why: `no baseline ${path.relative(REPO, file)}; run: node tests/playwright/visual-baselines.mjs update` };
  const r = await diffPng(browser, png, readFileSync(file));
  const ok = !r.sizeMismatch && r.pct <= CFG.screenshots.max_diff_pct;
  if (!ok) {
    mkdirSync(REPORT_DIR, { recursive: true });
    writeFileSync(path.join(REPORT_DIR, `${name}.actual.png`), png);
    if (r.diff) writeFileSync(path.join(REPORT_DIR, `${name}.diff.png`), Buffer.from(r.diff, 'base64'));
  }
  return { name, ok, pct: r.pct, diffPixels: r.diffPixels, why: r.sizeMismatch ? `size ${r.actual} vs baseline ${r.expected}` : `${r.pct.toFixed(4)}% of pixels differ (budget ${CFG.screenshots.max_diff_pct}%)` };
}

/** All (surface, viewport, scheme) combinations: light everywhere, dark at every viewport (autospec requires both themes). */
export const combos = (filter = '') => SURFACES.filter((s) => s.slug.includes(filter)).flatMap((s) => VIEWPORTS.flatMap((v) => ['light', 'dark'].map((scheme) => ({ surface: s, viewport: v, scheme }))));

async function main() {
  const [cmd, filter = ''] = process.argv.slice(2);
  if (!['update', 'check'].includes(cmd)) { console.error('usage: visual-baselines.mjs update|check [slug-filter]'); process.exit(2); }
  const srv = await newDeterministicServer();
  const browser = await launch();
  let failed = 0;
  try {
    mkdirSync(BASELINE_DIR, { recursive: true });
    for (const { surface, viewport, scheme } of combos(filter)) {
      const png = await capture(browser, srv.info, surface, viewport, scheme);
      if (cmd === 'update') { writeFileSync(fileFor(surface.slug, viewport, scheme), png); console.log(`wrote ${path.relative(REPO, fileFor(surface.slug, viewport, scheme))} (${png.length} bytes)`); continue; }
      const r = await checkAgainstBaseline(browser, png, surface.slug, viewport, scheme);
      console.log(`${r.ok ? 'ok  ' : 'FAIL'} ${r.name}: ${r.why}`);
      if (!r.ok) failed += 1;
    }
  } finally { await browser.close(); srv.stop(); }
  if (failed) { console.error(`${failed} visual regression(s); diffs are in ${path.relative(REPO, REPORT_DIR)}`); process.exit(1); }
}
if (import.meta.url === `file://${process.argv[1]}`) main();
