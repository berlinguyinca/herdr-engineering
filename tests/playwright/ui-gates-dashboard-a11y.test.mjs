// T16 UI gates: accessibility of the dashboard surfaces that have visual baselines (visual-dashboard.mjs): the Missions page and the
// assistant Proposals, Notifications and Approvals tabs. Same audits as ui-gates-states.test.mjs (static rules, focus indicators, reflow at
// phone width), over the same deterministic fixtures and the same steps the screenshots use. Each state must really render (a floor on
// audited controls and text nodes), so an empty page cannot pass.
//
// Run: node --test --test-concurrency=1 tests/playwright/ui-gates-dashboard-a11y.test.mjs
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { CFG, launch } from './ui-gates-lib.mjs';
import { staticAudit, focusIndicatorAudit, overflowAudit, overflowFailure, summarize } from './a11y-checks.mjs';
import { DASHBOARD_SURFACES, openDashboard } from './visual-dashboard.mjs';

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser?.close(); });

// least number of controls / text nodes each state must have audited (about 90% of what the audit counted when these were set)
const FLOOR = { 'dashboard-missions': { controls: 10, text: 40 }, 'dashboard-assistant-proposals': { controls: 25, text: 60 },
  'dashboard-assistant-notifications': { controls: 20, text: 45 }, 'dashboard-assistant-approvals': { controls: 20, text: 45 } };
const KNOWN = {   // slug -> run label -> rule -> recorded count (measured 2026-10-29; identical on repeated runs)
  'dashboard-missions': {
    'desktop light': {'color-contrast': 3, 'focus-indicator-contrast': 12},
    'desktop dark': {'color-contrast': 2},
    'phone (375px) light': {'color-contrast': 3, 'focus-indicator-contrast': 12},
  },
  'dashboard-assistant-proposals': {
    'desktop light': {'name-role-value': 1, 'heading-order': 1, 'label': 1, 'color-contrast': 4, 'focus-indicator-contrast': 1, 'focus-visible': 17},
    'desktop dark': {'name-role-value': 1, 'heading-order': 1, 'label': 1, 'color-contrast': 2, 'focus-visible': 17},
    'phone (375px) light': {'name-role-value': 1, 'heading-order': 1, 'label': 1, 'color-contrast': 4, 'focus-indicator-contrast': 1, 'focus-visible': 17},
  },
  'dashboard-assistant-notifications': {
    'desktop light': {'name-role-value': 1, 'label': 1, 'color-contrast': 6, 'focus-indicator-contrast': 1, 'focus-visible': 10},
    'desktop dark': {'name-role-value': 1, 'label': 1, 'color-contrast': 2, 'focus-visible': 10},
    'phone (375px) light': {'name-role-value': 1, 'label': 1, 'color-contrast': 6, 'focus-indicator-contrast': 1, 'focus-visible': 10},
  },
  'dashboard-assistant-approvals': {
    'desktop light': {'name-role-value': 1, 'label': 1, 'color-contrast': 4, 'focus-indicator-contrast': 1, 'focus-visible': 10},
    'desktop dark': {'name-role-value': 1, 'label': 1, 'color-contrast': 2, 'focus-visible': 10},
    'phone (375px) light': {'name-role-value': 1, 'label': 1, 'color-contrast': 4, 'focus-indicator-contrast': 1, 'focus-visible': 10},
  },
};
const RUNS = [['desktop light', 'desktop', 'light'], ['desktop dark', 'desktop', 'dark'], ['phone (375px) light', 'mobile', 'light']];

for (const [label, viewport, colorScheme] of RUNS) {
  test(`dashboard surfaces are accessible: ${label}`, async () => {
    const bad = [];
    for (const s of DASHBOARD_SURFACES) {
      const { ctx, page } = await openDashboard(browser, s.hash, { viewport: CFG.viewports[viewport], colorScheme, reducedMotion: 'reduce', hasTouch: viewport === 'mobile' });
      try {
        await page.locator(s.proof).first().waitFor();
        await s.steps(page);
        await page.waitForTimeout(300);
        const stat = await staticAudit(page, '#view');
        const foc = await focusIndicatorAudit(page, '#view');
        console.log(`# audited ${label} | ${s.slug} | controls ${stat.stats.controls} | focused ${foc.stats.focused} | text ${stat.stats.textNodes}`);
        const floor = FLOOR[s.slug];
        if (stat.stats.controls < floor.controls) bad.push(`[${s.slug}] only ${stat.stats.controls} controls audited, expected at least ${floor.controls}`);
        if (stat.stats.textNodes < floor.text) bad.push(`[${s.slug}] only ${stat.stats.textNodes} text nodes audited, expected at least ${floor.text}`);
        const f = [...stat, ...foc];
        const byRule = {}; for (const x of f) byRule[x.rule] = (byRule[x.rule] || 0) + 1;
        console.log(`# findings ${label} | ${s.slug} | ${JSON.stringify(byRule)}`);
        // The dashboard app was never under the accessibility gates, so these pages have findings today. They are a RATCHET, not an
        // approval: a rule that is not listed, or a count above the recorded one, fails; a count that falls must be lowered here.
        const known = KNOWN[s.slug]?.[label] || {};
        const worse = Object.entries(byRule).filter(([rule, n]) => n > (known[rule] || 0));
        if (worse.length) bad.push(`[${s.slug}] new or more findings than the recorded ratchet: ${worse.map(([r, n]) => `${r} ${n} > ${known[r] || 0}`).join(', ')}\n${summarize(f)}`);
        const better = Object.entries(known).filter(([rule, n]) => (byRule[rule] || 0) < n);
        if (better.length) bad.push(`[${s.slug}] findings fell (${better.map(([r, n]) => `${r} ${byRule[r] || 0} < ${n}`).join(', ')}): lower KNOWN in this file`);
        if (viewport === 'mobile') { const audit = await overflowAudit(page);
          // the freshness dot is an 8px box whose text label is moved off-screen on purpose (a text-indent technique): not content that is cut
          audit.clipped = audit.clipped.filter((c) => !c.startsWith('herdr-freshness '));
          const o = overflowFailure(audit); if (o) bad.push(`[${s.slug}] reflow: ${o}`); }
      } finally { await ctx.close(); }
    }
    assert.deepEqual(bad, [], `\n${bad.join('\n')}`);
  });
}
