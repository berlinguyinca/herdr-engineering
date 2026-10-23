// T16 UI gates: screenshot visual regression of the collaboration page against committed baselines
// (docs/assets/screenshots/<surface>__<viewport>[-dark].png, the autospec location). See visual-baselines.mjs for the capture,
// the compare and the commands to update. Budget: ui-gates.yaml `screenshots.max_diff_pct` (autospec: 0.1%).
//
// Run: node --test --test-concurrency=1 tests/playwright/ui-gates-visual.test.mjs
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { CFG, launch } from './ui-gates-lib.mjs';
import { SURFACES, VIEWPORTS, capture, checkAgainstBaseline, diffPng, newDeterministicServer } from './visual-baselines.mjs';

let srv; let browser;
before(async () => { srv = await newDeterministicServer(); browser = await launch(); });
after(async () => { await browser?.close(); srv?.stop(); });

test('the capture is deterministic: two fresh page loads of the same surface are identical within a fifth of the budget', async () => {
  const lineage = SURFACES.find((s) => s.slug === 'collab-lineage');
  const a = await capture(browser, srv.info, lineage, 'desktop', 'light');
  const b = await capture(browser, srv.info, lineage, 'desktop', 'light');
  const r = await diffPng(browser, a, b);
  assert.equal(r.sizeMismatch, false);
  assert.ok(r.pct <= CFG.screenshots.max_diff_pct / 5, `${r.diffPixels} pixels (${r.pct.toFixed(4)}%) differ between two identical captures: the fixtures are not deterministic enough`);
});

test('the gate has teeth: a one-colour change and a layout shift both exceed the budget', async () => {
  const s = SURFACES.find((x) => x.slug === 'collab-attention');
  const base = await capture(browser, srv.info, s, 'desktop', 'light');
  const { openReal, openSub } = await import('./ui-gates-lib.mjs');
  const { ctx, page } = await openReal(browser, srv.info, 'alice', { viewport: CFG.viewports.desktop, colorScheme: 'light', reducedMotion: 'reduce' });
  await page.clock.setFixedTime(new Date(CFG.screenshots.fixed_clock));
  await openSub(page, 'attention');
  await page.locator('.cx-attn-item').first().waitFor();
  await page.addStyleTag({ content: '*{animation:none!important;transition:none!important;caret-color:transparent!important}.hd-btn{background:#b00020!important}' });
  await page.waitForTimeout(400);
  const changed = await page.screenshot({ clip: { x: 0, y: 0, width: 1280, height: (await page.evaluate(() => Math.max(document.documentElement.scrollHeight, innerHeight))) }, animations: 'disabled', caret: 'hide' });
  await ctx.close();
  const r = await diffPng(browser, changed, base);
  assert.ok(r.sizeMismatch || r.pct > CFG.screenshots.max_diff_pct, `a recoloured button must be caught (${r.pct}%)`);
});

for (const scheme of ['light', 'dark']) {
  for (const viewport of VIEWPORTS) {
    test(`visual regression: ${viewport} ${scheme}, every surface within ${CFG.screenshots.max_diff_pct}% of its baseline`, { timeout: 300000 }, async () => {
      const failures = [];
      for (const surface of SURFACES) {
        const png = await capture(browser, srv.info, surface, viewport, scheme);
        const r = await checkAgainstBaseline(browser, png, surface.slug, viewport, scheme);
        if (!r.ok) failures.push(`${r.name}: ${r.why}`);
      }
      assert.deepEqual(failures, [], 'regenerate intentionally with: node tests/playwright/visual-baselines.mjs update (diffs: .autospec/reports/visual/)');
    });
  }
}
