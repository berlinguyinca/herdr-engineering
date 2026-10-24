// Screenshot baselines and visual-regression compare for the collaboration page (T16 UI gates).
//
// Conventions follow autospec (gen-screenshots.mjs / ux-ui-workstream.md / qa-visual-findings.sh):
//   * files:   docs/assets/screenshots/<surface-slug>__<viewport>[-dark].png          (whole page; viewports desktop 1280x800,
//              tablet 768x1024, mobile 375x667) and
//              docs/assets/screenshots/<surface-slug>--<region>__desktop[-dark].png   (one component: a graph, a node, the inspector,
//              the logo, the tab bar, a proposal review). Light AND dark are both required.
//   * budget:  a page diff above 0.01% of pixels fails, and so does a component diff above 0.05% of ITS pixels (ui-gates.yaml). A
//              whole-page percentage hides a small defect (an icon, a row of glyphs, all the edges of a graph), so every
//              component the page is made of is compared on its own as well.
//   * the page is made deterministic first: a fixed server clock, a fixed browser clock, reduced motion, animations disabled,
//     the caret hidden, server-generated ids rewritten to stable tokens, and the fonts BUNDLED (tests/playwright/fonts, SIL OFL)
//     instead of whatever the host has installed. Which font each element really used is asserted through the DevTools protocol.
//
// Commands (no environment variables; arguments only):
//   node tests/playwright/visual-baselines.mjs check                         compare everything with its baseline; exit 1 on any miss
//   node tests/playwright/visual-baselines.mjs check lineage                 only surfaces whose slug contains "lineage"
//   node tests/playwright/visual-baselines.mjs update --i-understand-this-rewrites-baselines [slug-filter]
//       rewrites PNGs from a fresh deterministic run. REFUSED unless the flag is given AND the git working tree is clean (the
//       baselines must come from committed code). It prints every file it wrote; review the PNG diff before committing.
// Baselines are tied to the Chromium build Playwright resolves (see docs/operations/ui-gates.md, "Baseline policy").
import { execFileSync } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync, existsSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';
import { CFG, REPO, launch, openReal, openSub, startRealServer } from './ui-gates-lib.mjs';

export const BASELINE_DIR = path.join(REPO, 'docs', 'assets', 'screenshots');
export const REPORT_DIR = path.join(REPO, '.autospec', 'reports', 'visual');
export const FONT_DIR = path.join(import.meta.dirname, 'fonts');
export const VIEWPORTS = ['desktop', 'tablet', 'mobile'];
export const UPDATE_FLAG = '--i-understand-this-rewrites-baselines';
// Pages are captured to their FULL height. A page taller than this is an error, never silently clipped: the part below a clip was
// once never compared at all (the whole workbench of a selected node sat below 2400px). Raise the cap or add component regions.
const MAX_HEIGHT = 10000;

const selectNode = async (page) => { await page.locator('g[data-node="attempt:a1"]').focus(); await page.keyboard.press('Enter'); await page.locator('.wb-comment').first().waitFor(); await page.locator('.wb-steer').first().waitFor(); await page.evaluate(() => document.activeElement?.blur()); };

/** The surfaces under test: slug, how to reach it, a selector that proves it rendered, optional steps, and components compared on their own. */
export const SURFACES = [
  { slug: 'collab-rooms', hash: '/rooms', proof: '#hcn-rooms' },
  { slug: 'collab-chat', sub: 'chat', proof: '#hcn-log', regions: [{ name: 'logo', selector: '.cx-logo' }, { name: 'subtabs', selector: '.cx-stabs' }, { name: 'log', selector: '#hcn-log' }] },
  { slug: 'collab-tabs', sub: 'tabs', proof: '#hcn-tabs [role=tab]' },
  { slug: 'collab-lineage', sub: 'lineage', proof: 'svg.lin-svg g[data-node]', steps: selectNode, resetScroll: true,
    regions: [{ name: 'graph', selector: '.lin-scroll' }, { name: 'node', selector: 'g[data-node="attempt:a1"]' }, { name: 'inspector', selector: '.lin-inspector' },
      // the workbench of a selected node: comments, fork table, steering form, requests. Each is its own region because a
      // paragraph of text or one button is far below the page-level budget (a transparent first comment scored 0.0025%).
      { name: 'wbcomments', selector: 'ul.wb-comments' }, { name: 'wbfork', selector: '.wb-table-wrap' }, { name: 'wbpropose', selector: 'form[data-wb-form="propose"]' }, { name: 'wbsteers', selector: 'ul.wb-steers' }] },
  { slug: 'collab-lineage-gantt', sub: 'lineage', proof: 'svg.lin-svg', resetScroll: true, steps: async (page) => { await page.locator('[data-view="gantt"]').click(); await page.locator('svg.lin-svg[role=img]').waitFor(); },
    regions: [{ name: 'gantt', selector: 'svg.lin-svg[role=img]' }] },
  { slug: 'collab-flow', sub: 'flow', proof: '#hcn-flow svg.cx-flow', regions: [{ name: 'flowhead', selector: '#hcn-p-flow .cx-row--between' }] },
  { slug: 'collab-attention', sub: 'attention', proof: '.cx-attn-item', regions: [{ name: 'item', selector: '.cx-attn-item' }] },
  { slug: 'collab-progress', sub: 'progress', proof: '#hcn-p-progress' },
  { slug: 'collab-links', sub: 'links', proof: '#hcn-links li' },
  { slug: 'collab-proposals', sub: 'proposals', proof: '[data-proposal]', steps: async (page) => { await page.locator('[data-proposal]').first().click(); await page.locator('#hcn-review [data-decide]').first().waitFor(); },
    regions: [{ name: 'review', selector: '#hcn-review' }] },
];

export const fileFor = (slug, viewport, scheme) => path.join(BASELINE_DIR, `${slug}__${viewport}${scheme === 'dark' ? '-dark' : ''}.png`);
export const regionFileFor = (slug, region, scheme) => fileFor(`${slug}--${region}`, 'desktop', scheme);

// Server-generated ids are random per run; rewrite them to stable tokens so the pixels are comparable.
const NORMALIZE = () => {
  const rules = [[/\b(collab|prp|agent|room|msg|task|dlg|lnk|link|evt)_[0-9a-z]{16,}\b/g, '$1_ID'], [/(@?)\bext-[a-z0-9-]*?-[a-z0-9]{4}\b/g, '$1ext-agent-xxxx'],
    [/\bkey:[0-9a-f]{12}\b/g, 'key:KEYID'], [/\b[0-9a-f]{16,64}\b/g, (m) => 'HEX'.padEnd(Math.min(m.length, 12), 'x')], [/\bik-[0-9a-f]+\b/g, 'ik-KEY'],
    // a text node that BEGINS with 8 to 15 hex characters is a short digest (the steering fence is shown as its first 10): random per run
    [/^[0-9a-f]{8,15}(?=…|\.{3}|$)/, (m) => 'HEX'.padEnd(m.length, 'x')]];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n; while ((n = walker.nextNode())) { let v = n.nodeValue; for (const [re, to] of rules) v = v.replace(re, to); if (v !== n.nodeValue) n.nodeValue = v; }
};

export async function newDeterministicServer() {
  return startRealServer({ fixed_clock: true });
}

// ---- fonts -----------------------------------------------------------------------------------------------------------
const b64 = (f) => readFileSync(path.join(FONT_DIR, f)).toString('base64');
let fontCss = null;
/**
 * @font-face rules that map the design system's first-choice families onto bundled subsets: Liberation for text, and, through
 * unicode-range on the SAME family names, a symbol font for the geometric shapes, dingbats and media glyphs the page paints
 * (so a glyph the text face lacks never falls through to whatever the host has installed).
 */
export function bundledFontCss() {
  if (fontCss) return fontCss;
  const face = (family, weight, file, range = '') => `@font-face{font-family:"${family}";font-style:normal;font-weight:${weight};src:url(data:font/ttf;base64,${b64(file)}) format("truetype")${range ? `;unicode-range:${range}` : ''}}`.replace('format("truetype");', 'format("truetype");');
  const SYMBOLS = 'U+2010-2027,U+2190-21FF,U+2212,U+25A0-25FF,U+2600-27BF';
  const MEDIA = 'U+23E9-23F3';
  const rules = [];
  for (const fam of ['Inter', 'JetBrains Mono']) {
    const mono = fam === 'JetBrains Mono';
    rules.push(face(fam, '100 599', mono ? 'LiberationMono-Regular.subset.ttf' : 'LiberationSans-Regular.subset.ttf'));
    rules.push(face(fam, '600 900', mono ? 'LiberationMono-Bold.subset.ttf' : 'LiberationSans-Bold.subset.ttf'));
    rules.push(face(fam, '100 599', 'HerdrSymbols-Regular.subset.ttf', SYMBOLS), face(fam, '600 900', 'HerdrSymbols-Bold.subset.ttf', SYMBOLS));
    // two weight ranges, like the other faces: a face whose range differs from its siblings' is not merged into the family's coverage
    rules.push(face(fam, '100 599', 'HerdrSymbols2-Regular.subset.ttf', MEDIA), face(fam, '600 900', 'HerdrSymbols2-Regular.subset.ttf', MEDIA));
  }
  fontCss = rules.join('\n');
  return fontCss;
}

/** Ask Chromium which font file really painted EVERY element (and ::before / ::after) with text: each must be a bundled font. */
export async function assertBundledFonts(page, ctx) {
  const cdp = await ctx.newCDPSession(page);
  try {
    await cdp.send('DOM.enable'); await cdp.send('CSS.enable');
    const { root } = await cdp.send('DOM.getDocument', { depth: -1, pierce: true });
    const ids = [];
    const walk = (n) => {
      if (n.nodeType === 1 && !['SCRIPT', 'STYLE', 'HEAD', 'HTML', 'META', 'LINK', 'TITLE'].includes(n.nodeName)) ids.push(n.nodeId);
      for (const c of [...(n.children || []), ...(n.pseudoElements || []), ...(n.shadowRoots || [])]) walk(c);
    };
    walk(root);
    const host = new Map(); const used = new Map(); let checked = 0;
    for (const nodeId of ids) {
      let fonts; try { ({ fonts } = await cdp.send('CSS.getPlatformFontsForNode', { nodeId })); } catch { continue; }
      for (const f of fonts) {
        if (!f.glyphCount) continue;
        checked += 1;
        if (f.isCustomFont) used.set(f.familyName, (used.get(f.familyName) || 0) + f.glyphCount);
        else host.set(f.familyName, (host.get(f.familyName) || 0) + f.glyphCount);
      }
    }
    if (host.size) throw new Error(`screenshots must use the bundled fonts only, but these host fonts painted glyphs: ${[...host].map(([f, n]) => `${f} (${n} glyphs)`).join(', ')}`);
    if (!used.size) throw new Error('could not read which fonts painted the page');
    return { checked, used: Object.fromEntries(used) };
  } finally { await cdp.detach().catch(() => {}); }
}

/**
 * Capture one surface at one viewport / colour scheme. Returns { page: Buffer, regions: {name: Buffer} }.
 * `extraCss` is only for the gate's own "teeth" tests: it lets a test inject a defect and prove the compare notices it.
 */
export async function capture(browser, info, surface, viewport, scheme, { extraCss = '', regions = viewport === 'desktop' } = {}) {
  const { ctx, page } = await openReal(browser, info, 'alice', { viewport: CFG.viewports[viewport], colorScheme: scheme, reducedMotion: 'reduce', deviceScaleFactor: 1, bypassCSP: true });
  try {
    await page.clock.setFixedTime(new Date(CFG.screenshots.fixed_clock));
    await page.addStyleTag({ content: bundledFontCss() });
    if (surface.hash) { await page.evaluate((h) => { window.location.hash = h; }, surface.hash); } else { await openSub(page, surface.sub); }
    await page.locator(surface.proof).first().waitFor({ timeout: 15000 });
    if (surface.steps) await surface.steps(page);
    await page.addStyleTag({ content: '*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important;scroll-behavior:auto!important}' });
    if (extraCss) await page.addStyleTag({ content: extraCss });
    await page.evaluate(() => document.fonts.ready);
    await page.waitForTimeout(600);
    // the page re-renders on its poll and puts the random text back: keep rewriting it for as long as the page lives (a one-off pass raced
    // the redraw and left an unnormalised digest in about half of the captures)
    await page.evaluate((src) => { const fn = new Function(`return (${src})`)(); fn(); new MutationObserver(() => fn()).observe(document.body, { subtree: true, childList: true, characterData: true }); }, NORMALIZE.toString());
    // selecting a node scrolls the page by a run-dependent pixel; those surfaces are captured from the top (the chat, which scrolls to
    // its newest message on purpose, is not touched)
    if (surface.resetScroll) { await page.evaluate(() => { window.scrollTo(0, 0); document.scrollingElement.scrollTop = 0; }); await page.waitForTimeout(100); }
    await assertBundledFonts(page, ctx);
    const full = await page.evaluate(() => Math.max(document.documentElement.scrollHeight, innerHeight));
    if (full > MAX_HEIGHT) throw new Error(`${surface.slug} at ${viewport} is ${full}px tall, above the ${MAX_HEIGHT}px capture cap: the rest would never be compared`);
    const height = full;
    const out = { page: await page.screenshot({ clip: { x: 0, y: 0, width: CFG.viewports[viewport].width, height }, animations: 'disabled', caret: 'hide' }), regions: {} };
    if (regions) {
      for (const r of surface.regions || []) {
        // by bounding box, not locator.screenshot(): that waits for the element to be visible, which would make a test that hides
        // the element (the gate's own teeth tests) time out instead of producing the picture that must fail the compare
        const loc = page.locator(r.selector).first();
        await loc.scrollIntoViewIfNeeded();
        const box = await loc.boundingBox();
        const off = await page.evaluate(() => ({ x: window.scrollX, y: window.scrollY }));
        out.regions[r.name] = await page.screenshot({ clip: { x: box.x + off.x, y: box.y + off.y, width: box.width, height: box.height }, fullPage: true, animations: 'disabled', caret: 'hide' });
      }
    }
    return out;
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

/** Compare a PNG with a baseline file. A page is held to `max_diff_pct`, a component to `component_max_diff_pct` of its own area. */
export async function checkFile(browser, png, file, budget) {
  const name = path.basename(file, '.png');
  if (!existsSync(file)) return { name, ok: false, pct: 100, why: `no baseline ${path.relative(REPO, file)}; run: node tests/playwright/visual-baselines.mjs update ${UPDATE_FLAG} ${name.split('__')[0]}` };
  const r = await diffPng(browser, png, readFileSync(file));
  const ok = !r.sizeMismatch && r.pct <= budget;
  if (!ok) {
    mkdirSync(REPORT_DIR, { recursive: true });
    writeFileSync(path.join(REPORT_DIR, `${name}.actual.png`), png);
    if (r.diff) writeFileSync(path.join(REPORT_DIR, `${name}.diff.png`), Buffer.from(r.diff, 'base64'));
  }
  return { name, ok, pct: r.pct, diffPixels: r.diffPixels, why: r.sizeMismatch ? `size ${r.actual} vs baseline ${r.expected}` : `${r.pct.toFixed(4)}% of pixels differ (budget ${budget}%)` };
}
export const checkAgainstBaseline = (browser, png, slug, viewport, scheme) => checkFile(browser, png, fileFor(slug, viewport, scheme), CFG.screenshots.max_diff_pct);
export const checkRegion = (browser, png, slug, region, scheme) => checkFile(browser, png, regionFileFor(slug, region, scheme), CFG.screenshots.component_max_diff_pct);

/** All (surface, viewport, scheme) combinations: light everywhere, dark at every viewport (autospec requires both themes). */
export const combos = (filter = '') => SURFACES.filter((s) => s.slug.includes(filter)).flatMap((s) => VIEWPORTS.flatMap((v) => ['light', 'dark'].map((scheme) => ({ surface: s, viewport: v, scheme }))));

/** Every PNG the suite owns: a page per (surface, viewport, scheme) and a component per (surface, region, scheme). */
export function expectedBaselineFiles() {
  const out = new Set();
  for (const s of SURFACES) for (const scheme of ['light', 'dark']) {
    for (const v of VIEWPORTS) out.add(path.basename(fileFor(s.slug, v, scheme)));
    for (const r of s.regions || []) out.add(path.basename(regionFileFor(s.slug, r.name, scheme)));
  }
  return out;
}
/** PNGs in the baseline folder that no surface produces any more: they would sit there looking like coverage. Never deleted by the tool. */
export function orphanBaselines(dir = BASELINE_DIR) {
  const want = expectedBaselineFiles();
  return existsSync(dir) ? readdirSync(dir).filter((f) => f.endsWith('.png') && !want.has(f)).sort() : [];
}

/** Total size of the committed baselines, and the biggest file: the policy caps both (ui-gates.yaml screenshots.max_total_kb / max_file_kb). */
export function baselineSizes() {
  const files = existsSync(BASELINE_DIR) ? readdirSync(BASELINE_DIR).filter((f) => f.endsWith('.png')).map((f) => ({ f, kb: statSync(path.join(BASELINE_DIR, f)).size / 1024 })) : [];
  return { count: files.length, totalKb: files.reduce((a, b) => a + b.kb, 0), biggest: files.sort((a, b) => b.kb - a.kb)[0] || null };
}

const git = (...args) => execFileSync('git', args, { cwd: REPO, encoding: 'utf8' });
function refuseUnlessAllowed(argv) {
  if (!argv.includes(UPDATE_FLAG)) {
    console.error(`refusing to rewrite the screenshot baselines: this overwrites up to ${combos().length + 16} PNGs that reviewers compare against.\nIf you mean it, run: node tests/playwright/visual-baselines.mjs update ${UPDATE_FLAG} [slug-filter]`);
    process.exit(2);
  }
  const dirty = git('status', '--porcelain').trim();
  if (dirty) {
    console.error(`refusing to rewrite the screenshot baselines from uncommitted code: the baselines must come from a commit anyone can check out.\nCommit or stash first. Uncommitted:\n${dirty}`);
    process.exit(2);
  }
}

async function main() {
  const argv = process.argv.slice(2);
  const [cmd, ...rest] = argv;
  const filter = rest.find((a) => !a.startsWith('--')) || '';
  if (!['update', 'check'].includes(cmd)) { console.error(`usage: visual-baselines.mjs check [slug-filter] | update ${UPDATE_FLAG} [slug-filter]`); process.exit(2); }
  if (!combos(filter).length) { console.error(`no surface matches "${filter}" (surfaces: ${SURFACES.map((x) => x.slug).join(', ')})`); process.exit(2); }
  if (cmd === 'update') refuseUnlessAllowed(argv);
  const srv = await newDeterministicServer();
  const browser = await launch();
  let failed = 0;
  try {
    mkdirSync(BASELINE_DIR, { recursive: true });
    for (const { surface, viewport, scheme } of combos(filter)) {
      const shot = await capture(browser, srv.info, surface, viewport, scheme);
      const targets = [[fileFor(surface.slug, viewport, scheme), shot.page, (png) => checkAgainstBaseline(browser, png, surface.slug, viewport, scheme)],
        ...Object.entries(shot.regions).map(([name, png]) => [regionFileFor(surface.slug, name, scheme), png, (p) => checkRegion(browser, p, surface.slug, name, scheme)])];
      for (const [file, png, compare] of targets) {
        if (cmd === 'update') {
          const kb = png.length / 1024;
          if (kb > CFG.screenshots.max_file_kb) { console.error(`${path.basename(file)} is ${kb.toFixed(0)} KB, above the ${CFG.screenshots.max_file_kb} KB cap: shrink the surface or raise the cap in ui-gates.yaml on purpose`); failed += 1; continue; }
          writeFileSync(file, png); console.log(`wrote ${path.relative(REPO, file)} (${png.length} bytes)`); continue;
        }
        const r = await compare(png);
        console.log(`${r.ok ? 'ok  ' : 'FAIL'} ${r.name}: ${r.why}`);
        if (!r.ok) failed += 1;
      }
    }
    const orphans = orphanBaselines();
    if (orphans.length) {
      console.error(`${cmd === 'check' && !filter ? 'FAIL' : 'note'} orphan baselines that no surface produces: ${orphans.join(', ')} (remove them by hand after review)`);
      if (cmd === 'check' && !filter) failed += orphans.length;
    }
    const sz = baselineSizes();
    console.log(`${sz.count} baselines, ${(sz.totalKb).toFixed(0)} KB total (cap ${CFG.screenshots.max_total_kb} KB), biggest ${sz.biggest ? `${sz.biggest.f} ${sz.biggest.kb.toFixed(0)} KB` : 'none'}`);
    if (sz.totalKb > CFG.screenshots.max_total_kb) { console.error('the baselines exceed the total size cap'); failed += 1; }
  } finally { await browser.close(); srv.stop(); }
  if (failed) { console.error(`${failed} problem(s); diffs are in ${path.relative(REPO, REPORT_DIR)}`); process.exit(1); }
}
if (import.meta.url === `file://${process.argv[1]}`) main();
