// T16 UI gates: accessibility of the STATES where the controls live (ui-gates-states.mjs): a selected lineage node with its
// comment, steering, fork, consult and link forms, the steering confirm step, the Gantt, the replay bar, the table view, a proposal
// under review, expanded attention items, a remote agent tab. The default view of each tab holds only a few of the page's controls.
// Every state must really open (a missing `must` element fails) and must have audited at least `min` controls, so a silent skip,
// an empty scope or a state that never rendered cannot pass.
//
// Run: node --test --test-concurrency=1 tests/playwright/ui-gates-states.test.mjs
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { CFG, launch, openReal, startRealServer } from './ui-gates-lib.mjs';
import { staticAudit, focusIndicatorAudit, summarize } from './a11y-checks.mjs';
import { STATES } from './ui-gates-states.mjs';

let srv; let browser;
before(async () => { srv = await startRealServer({}); browser = await launch(); });
after(async () => { await browser?.close(); srv?.stop(); });

const RUNS = [
  ['desktop light', { viewport: CFG.viewports.desktop, colorScheme: 'light' }],
  ['desktop dark', { viewport: CFG.viewports.desktop, colorScheme: 'dark' }],
  ['phone (375px) light', { viewport: CFG.viewports.mobile, colorScheme: 'light', hasTouch: true }],
];

for (const [label, opts] of RUNS) {
  test(`every control in every state is accessible: ${label}`, async () => {
    const bad = []; let audited = 0; let focused = 0;
    // a fresh page per state: states must not depend on what an earlier one left open
    for (const st of STATES) {
      const { ctx, page } = await openReal(browser, srv.info, 'alice', { ...opts, reducedMotion: 'reduce' });
      try {
        await st.open(page);
        for (const sel of st.must) assert.ok(await page.locator(sel).count() > 0, `[${st.id}] did not reach its state: "${sel}" is missing`);
        const stat = await staticAudit(page, st.scope);
        const foc = await focusIndicatorAudit(page, st.scope);
        audited += stat.stats.controls; focused += foc.stats.focused;
        if (stat.stats.controls < st.min) bad.push(`[${st.id}] only ${stat.stats.controls} controls audited, expected at least ${st.min}: the state did not render or the scope is wrong`);
        const f = [...stat, ...foc];
        if (f.length) bad.push(`[${st.id}] ${summarize(f)}`);
      } finally { await ctx.close(); }
    }
    assert.deepEqual(bad, [], `\n${bad.join('\n')}`);
    assert.ok(audited >= 150 && focused >= 100, `the audit really covered the page: ${audited} controls, ${focused} focused`);
  });
}
