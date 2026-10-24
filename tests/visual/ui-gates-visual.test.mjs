// T16 UI gates: screenshot visual regression of the collaboration page against committed baselines
// (docs/assets/screenshots, the autospec location). See tests/playwright/visual-baselines.mjs for capture, compare and the update
// guard. Budgets: tests/playwright/ui-gates.yaml `screenshots` (page 0.01%, component 0.05% of its own area, per-channel tolerance 2).
//
// This suite lives OUTSIDE tests/playwright so a plain `npm test` / `npm run test:ui` does not run it: baselines are tied to one
// Chromium build and would fail wholesale on another machine. `npm run test:ui-gates` runs it.
//
// Run: node --test --test-concurrency=1 tests/visual/ui-gates-visual.test.mjs
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, readdirSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { CFG, launch, openReal, openSub } from '../playwright/ui-gates-lib.mjs';
import { FONT_DIR, SURFACES, VIEWPORTS, assertBundledFonts, baselineSizes, bundledFontCss, capture, expectedBaselineFiles, orphanBaselines, checkAgainstBaseline, checkRegion, diffPng, newDeterministicServer } from '../playwright/visual-baselines.mjs';

let srv; let browser;
before(async () => { srv = await newDeterministicServer(); browser = await launch(); });
after(async () => { await browser?.close(); srv?.stop(); });

const surface = (slug) => SURFACES.find((s) => s.slug === slug);

test('the capture is deterministic: three fresh loads of the same surfaces agree within half the page budget', async () => {
  for (const slug of ['collab-lineage', 'collab-chat']) {
    const shots = [];
    for (let i = 0; i < 3; i += 1) shots.push(await capture(browser, srv.info, surface(slug), 'desktop', 'light'));
    for (const other of shots.slice(1)) {
      const r = await diffPng(browser, other.page, shots[0].page);
      assert.equal(r.sizeMismatch, false);
      assert.ok(r.pct <= CFG.screenshots.max_diff_pct / 2, `${slug}: ${r.diffPixels} pixels (${r.pct.toFixed(4)}%) differ between identical captures`);
      for (const [name, png] of Object.entries(other.regions)) {
        const rr = await diffPng(browser, png, shots[0].regions[name]);
        assert.ok(rr.pct <= CFG.screenshots.component_max_diff_pct / 2, `${slug}--${name}: ${rr.diffPixels} pixels (${rr.pct.toFixed(4)}%) differ between identical captures`);
      }
    }
  }
});

test('the baselines respect the size policy (total and per file)', () => {
  const sz = baselineSizes();
  assert.ok(sz.count >= expectedBaselineFiles().size, `${sz.count} baselines`);
  assert.ok(sz.totalKb <= CFG.screenshots.max_total_kb, `${sz.totalKb.toFixed(0)} KB of baselines exceed the ${CFG.screenshots.max_total_kb} KB cap`);
  assert.ok(sz.biggest.kb <= CFG.screenshots.max_file_kb, `${sz.biggest.f} is ${sz.biggest.kb.toFixed(0)} KB, above ${CFG.screenshots.max_file_kb} KB`);
});

test('no baseline is an orphan, and the tool notices one', () => {
  assert.deepEqual(orphanBaselines(), [], 'every PNG in docs/assets/screenshots is produced by a surface of the suite');
  const dir = mkdtempSync(path.join(os.tmpdir(), 'orphans-'));
  const [one] = [...expectedBaselineFiles()];
  writeFileSync(path.join(dir, one), ''); writeFileSync(path.join(dir, 'collab-gone__desktop.png'), '');
  assert.deepEqual(orphanBaselines(dir), ['collab-gone__desktop.png']);
  assert.equal(readdirSync(dir).length, 2, 'the tool does not delete what it finds');
});

test('the CLI refuses a slug that matches nothing, for check and for update, instead of exiting quietly', () => {
  for (const args of [['check', 'no-such-surface'], ['update', '--i-understand-this-rewrites-baselines', 'no-such-surface']]) {
    const r = spawnSync(process.execPath, [path.join(FONT_DIR, '..', 'visual-baselines.mjs'), ...args], { encoding: 'utf8' });
    assert.equal(r.status, 2, `${args.join(' ')} exits 2 (got ${r.status})`);
    assert.match(r.stderr, /no surface matches "no-such-surface"/);
  }
});

test('the font assertion looks at every element: a host font on ANY element fails the capture', async () => {
  const { ctx, page } = await openReal(browser, srv.info, 'alice', { bypassCSP: true, reducedMotion: 'reduce' });
  try {
    await page.addStyleTag({ content: bundledFontCss() });
    await openSub(page, 'chat');
    await page.locator('#hcn-log').waitFor();
    const ok = await assertBundledFonts(page, ctx);
    assert.ok(ok.checked >= 20, `${ok.checked} font runs were checked`);
    assert.deepEqual(Object.keys(ok.used).sort().filter((f) => /Liberation|Herdr/.test(f)).length > 0, true, `the bundled fonts painted the page: ${JSON.stringify(ok.used)}`);
    for (const [css, text] of [["font-family:'DejaVu Sans'", 'host sans'], ["font-family:serif", 'host serif'], ['font-family:Inter', '\u{1F916}']]) {
      await page.evaluate(([c, t]) => { const s = document.createElement('span'); s.id = 'host-probe'; s.setAttribute('style', c); s.textContent = t; document.body.appendChild(s); }, [css, text]);
      await assert.rejects(() => assertBundledFonts(page, ctx), /host fonts painted glyphs/, `${css} / ${text} must be caught`);
      await page.evaluate(() => document.getElementById('host-probe').remove());
    }
  } finally { await ctx.close(); }
});

test('the baselines do not depend on the host: the whole suite passes under a fontconfig that knows ONLY the bundled fonts', () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'fc-'));
  const conf = path.join(dir, 'fonts.conf');
  // rendering settings (hinting, antialiasing, subpixel) still come from the system's conf.d: they are NOT a font, and they are the
  // part of the host that a baseline can still depend on (documented in docs/operations/ui-gates.md)
  writeFileSync(conf, `<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig><dir>${FONT_DIR}</dir><cachedir>${path.join(dir, 'cache')}</cachedir>`
    + '<include ignore_missing="yes">/etc/fonts/conf.d</include></fontconfig>');
  const env = { ...process.env, FONTCONFIG_FILE: conf };
  const fc = spawnSync('fc-list', ['--format', '%{family}\\n'], { encoding: 'utf8', env });
  if (!fc.error) {
    const families = [...new Set(fc.stdout.split('\n').map((l) => l.trim()).filter(Boolean))];
    assert.ok(families.length > 0 && families.every((f) => /Liberation|Herdr/.test(f)), `only the bundled fonts are visible: ${families.join(' | ')}`);
  }
  const r = spawnSync(process.execPath, [path.join(FONT_DIR, '..', 'visual-baselines.mjs'), 'check'], { encoding: 'utf8', env, maxBuffer: 1 << 26, timeout: 900000 });
  assert.equal(r.status, 0, `baselines:check under the bundled-fonts-only fontconfig failed:\n${(r.stdout + r.stderr).split('\n').filter((l) => !l.startsWith('ok')).slice(0, 25).join('\n')}`);
});

// ---- the gate has teeth: each defect below is one a reviewer found slipping past an earlier, looser budget, or one a person would
// not miss. A defect is injected with CSS after the page is ready and the compare must fail it on the page or on the component.
const TEETH = [
  ['every edge of the lineage graph is hidden', 'collab-lineage', '.lin-edge, .lin-edge-line, .lin-arrowhead, .lin-edge-tag { visibility: hidden !important }', ['graph']],
  ['the inspector text is transparent', 'collab-lineage', '.lin-inspector, .lin-inspector * { color: transparent !important }', ['inspector']],
  ['the sidebar logo is hidden', 'collab-chat', '.cx-logo { visibility: hidden !important }', ['logo']],
  ['the node status glyphs are hidden', 'collab-lineage', '.lin-node tspan { visibility: hidden !important }', ['node', 'graph']],
  ['the sub-tab badge counts are gone', 'collab-chat', '.cx-badge { display: none !important }', ['subtabs']],
  ['the Send button is invisible', 'collab-chat', '.cx-compose .hd-btn, [data-send] { visibility: hidden !important }', []],
  ['the chat text is a few shades lighter (+7 per channel)', 'collab-chat', '.cx-msg, .cx-log * { color: #5d5d75 !important }', ['log']],
  ['the chat log is one pixel lower', 'collab-chat', '.cx-log { transform: translateY(1px) }', []],
  ['the accent colour moved by about 5 per channel', 'collab-attention', '.cx-page, .cx-embed { --accent: #0f7a5e !important }', []],
  ['buttons are one pixel wider', 'collab-attention', '.hd-btn { padding: 8px 15px !important }', ['item']],
  ['cards gained a pixel of padding', 'collab-attention', '.hd-card { padding: 17px !important }', []],
  ['the sub-tab bar gained 4px of padding', 'collab-attention', '.cx-stabs { padding-top: 4px !important }', []],
  ['the Pause motion label is gone (the button is an empty box)', 'collab-flow', '#hcn-flow-still { font-size: 0 !important; width: 10px; padding: 0 !important; overflow: hidden }', ['flowhead']],
  ['the first comment of the workbench is transparent', 'collab-lineage', 'p.wb-text { color: transparent !important; -webkit-text-fill-color: transparent !important }', ['wbcomments']],
  ['the fork button is hidden', 'collab-lineage', '.wb-table-wrap button { visibility: hidden !important }', ['wbfork']],
  ['the steering request buttons are hidden', 'collab-lineage', 'ul.wb-steers button { visibility: hidden !important }', ['wbsteers']],
  ['the steering form submit button is hidden', 'collab-lineage', 'form[data-wb-form="propose"] > button { visibility: hidden !important }', ['wbpropose']],
  ['the lineage node boxes lost their borders', 'collab-lineage', '.lin-box { stroke: transparent !important }', ['node', 'graph']],
  ['a failed node is painted the wrong colour', 'collab-lineage', '.lin-node--failed .lin-box { fill: #ccc !important }', ['graph']],
];
for (const [name, slug, css, regions] of TEETH) {
  test(`the gate has teeth: ${name}`, async () => {
    const s = surface(slug);
    const shot = await capture(browser, srv.info, s, 'desktop', 'light', { extraCss: css });
    const page = await checkAgainstBaseline(browser, shot.page, slug, 'desktop', 'light');
    const comp = [];
    for (const name2 of regions) comp.push(await checkRegion(browser, shot.regions[name2], slug, name2, 'light'));
    const caught = !page.ok || comp.some((c) => !c.ok);
    assert.ok(caught, `not caught: page ${page.pct.toFixed(4)}% (budget ${CFG.screenshots.max_diff_pct}%)${comp.map((c) => `, ${c.name} ${c.pct.toFixed(4)}%`).join('')}`);
    for (const c of comp) assert.ok(!c.ok, `the component ${c.name} should fail on its own (${c.pct.toFixed(4)}%)`);
  });
}

// ---- the compare itself: every surface at every viewport, light and dark, page and components
for (const scheme of ['light', 'dark']) {
  for (const viewport of VIEWPORTS) {
    test(`visual regression: ${viewport} ${scheme}, every surface within budget of its baseline`, async () => {
      const bad = [];
      for (const s of SURFACES) {
        const shot = await capture(browser, srv.info, s, viewport, scheme);
        const r = await checkAgainstBaseline(browser, shot.page, s.slug, viewport, scheme);
        if (!r.ok) bad.push(`${r.name}: ${r.why}`);
        for (const [name, png] of Object.entries(shot.regions)) {
          const c = await checkRegion(browser, png, s.slug, name, scheme);
          if (!c.ok) bad.push(`${c.name}: ${c.why}`);
        }
      }
      assert.deepEqual(bad, [], `\n${bad.join('\n')}\n(diffs: .autospec/reports/visual/)`);
    });
  }
}
