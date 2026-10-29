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
import { STATES, VIEWER_STATES } from './ui-gates-states.mjs';

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser?.close(); });

const RUNS = [
  ['desktop light', { viewport: CFG.viewports.desktop, colorScheme: 'light' }],
  ['desktop dark', { viewport: CFG.viewports.desktop, colorScheme: 'dark' }],
  ['phone (375px) light', { viewport: CFG.viewports.mobile, colorScheme: 'light', hasTouch: true }],
];

// What the audit counted when the floors were set (sum over STATES / over VIEWER_STATES). The totals may not fall below 90% of it.
// focused was 480 until the Usage page put its tables and charts behind tabs: a hidden panel's links are not tab stops, so one tab stop fewer is expected there.
const FLOOR = { owner: { controls: 540, focused: 470 }, viewer: { controls: 37, focused: 30 } };

for (const [label, opts] of RUNS) {
  test(`every control in every state is accessible: ${label}`, async () => {
    const bad = []; const sum = { owner: { controls: 0, focused: 0 }, viewer: { controls: 0, focused: 0 } };
    // acknowledging an attention item is server state, so each run gets its own server: a shared one ran out of items to acknowledge
    const srv = await startRealServer({ mention_agent: true });
    try {
      // a fresh page per state: states must not depend on what an earlier one left open
      for (const [who, user, states] of [['owner', 'alice', STATES], ['viewer', 'carol', VIEWER_STATES]]) {
        for (const st of states) {
          const { ctx, page } = await openReal(browser, srv.info, user, { ...opts, reducedMotion: 'reduce' });
          try {
            await st.open(page);
            for (const sel of st.must) assert.ok(await page.locator(sel).count() > 0, `[${st.id}] did not reach its state: "${sel}" is missing`);
            const stat = await staticAudit(page, st.scope);
            const foc = await focusIndicatorAudit(page, st.scope);
            sum[who].controls += stat.stats.controls; sum[who].focused += foc.stats.focused;
            console.log(`# audited ${label} | ${st.id.slice(0, 60)} | controls ${stat.stats.controls} | focused ${foc.stats.focused} | text ${stat.stats.textNodes}`);
            if (stat.stats.controls < st.min) bad.push(`[${st.id}] only ${stat.stats.controls} controls audited, expected at least ${st.min}: the state did not render or the scope is wrong`);
            if (stat.stats.textNodes < st.minText) bad.push(`[${st.id}] only ${stat.stats.textNodes} text nodes audited, expected at least ${st.minText}`);
            const f = [...stat, ...foc];
            if (f.length) bad.push(`[${st.id}] ${summarize(f)}`);
          } finally { await ctx.close(); }
        }
      }
      assert.deepEqual(bad, [], `\n${bad.join('\n')}`);
      for (const who of ['owner', 'viewer']) {
        assert.ok(sum[who].controls >= FLOOR[who].controls && sum[who].focused >= FLOOR[who].focused, `the ${who} audit really covered the page: ${sum[who].controls} controls (floor ${FLOOR[who].controls}), ${sum[who].focused} focused (floor ${FLOOR[who].focused})`);
      }
    } finally { srv.stop(); }
  });
}
