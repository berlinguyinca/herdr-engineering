// The UI states in which the controls actually live (T16 UI gates). A page-level audit of the default view of each tab never
// opens the inspector forms, the Gantt, the replay bar, the table view, the proposal review, the steering confirm step, the fork
// form, the consult/link forms, an expanded attention list or a remote tab, so it audited only a fraction of the controls.
// Each state below says how to reach it, which element must exist once it is open (a silent failure to open is a test failure,
// not a quiet skip) and the least number of controls that must have been audited inside its scope.
//
// Used by ui-gates-states.test.mjs (accessibility) and ui-gates-xss.test.mjs (inert hostile text).
import { openSub } from './ui-gates-lib.mjs';

const nodeSelected = async (page, node = 'attempt:a1') => {
  await openSub(page, 'lineage');
  await page.locator('svg.lin-svg g[data-node]').first().waitFor();
  await page.locator(`g[data-node="${node}"]`).focus();
  await page.keyboard.press('Enter');
  await page.locator('.wb-comment').first().waitFor();
  await page.locator('form[data-ctx-form="link"]').waitFor();
  await page.waitForTimeout(250);
};

/** @type {{id:string, scope:string, must:string[], min:number, open:(page:any)=>Promise<void>}[]} */
export const STATES = [
  { id: 'lineage: node selected (comment, steering, consult and link forms)', scope: '#hcn-p-lineage',
    must: ['form[data-wb-form="comment"]', 'form[data-wb-form="propose"]', 'form[data-ctx-form="ask"]', 'form[data-ctx-form="link"]'], min: 45, open: nodeSelected },
  { id: 'lineage: fork form (a base commit to fork from)', scope: '#hcn-p-lineage', must: ['select[data-wb-field="base_sha"]'], min: 45,
    open: async (page) => {
      await nodeSelected(page);
      await page.locator('input[data-wb-field="action"]').first().focus();
      for (let i = 0; i < 8 && !(await page.locator('select[data-wb-field="base_sha"]').count()); i += 1) await page.keyboard.press('ArrowDown');
    } },
  { id: 'lineage: steering confirm step (the request restated before it is sent)', scope: '#hcn-p-lineage', must: ['.wb-confirm', '[data-wb-act="confirm-go"]'], min: 45,
    open: async (page) => { await nodeSelected(page); await page.locator('[data-wb-act="confirm"]').first().click(); await page.locator('.wb-confirm').waitFor(); } },
  { id: 'lineage: Gantt view', scope: '#hcn-p-lineage', must: ['svg.lin-svg[role="img"]', '[data-view="gantt"][aria-selected="true"]'], min: 20,
    open: async (page) => { await openSub(page, 'lineage'); await page.locator('svg.lin-svg g[data-node]').first().waitFor(); await page.locator('[data-view="gantt"]').click(); await page.locator('svg.lin-svg[role="img"]').waitFor(); await page.waitForTimeout(250); } },
  { id: 'lineage: replay controls after stepping back', scope: '#hcn-p-lineage', must: ['[data-replay="slider"]', '[data-replay="live"]'], min: 20,
    open: async (page) => { await openSub(page, 'lineage'); await page.locator('svg.lin-svg g[data-node]').first().waitFor(); await page.locator('[data-replay="prev"]').click(); await page.waitForFunction(() => !/^Live/.test(document.querySelector('.lin-replay__status')?.textContent || 'Live')); } },
  { id: 'lineage: table alternative', scope: '#hcn-p-lineage', must: ['table.lin-table'], min: 20,
    open: async (page) => { await openSub(page, 'lineage'); await page.locator('svg.lin-svg g[data-node]').first().waitFor(); await page.locator('#hcn-lineage [data-mode="table"]').first().click(); await page.locator('table.lin-table').first().waitFor(); } },
  { id: 'proposals: a quarantined proposal under review', scope: '#hcn-p-proposals', must: ['#hcn-review [data-decide="accept"]', '#hcn-review [data-decide="reject"]'], min: 10,
    open: async (page) => { await openSub(page, 'proposals'); await page.locator('[data-proposal]').first().click(); await page.locator('#hcn-review [data-decide]').first().waitFor(); } },
  { id: 'attention: acknowledged items expanded', scope: '#hcn-p-attention', must: ['[data-attn-act="toggle-quiet"][aria-expanded="true"]'], min: 3,
    open: async (page) => {
      await openSub(page, 'attention');
      await page.locator('[data-attn-act="ack"]').first().click();
      await page.locator('[data-attn-act="toggle-quiet"]').waitFor();
      await page.locator('[data-attn-act="toggle-quiet"]').click();
      await page.locator('[data-attn-act="toggle-quiet"][aria-expanded="true"]').waitFor();
    } },
  { id: 'tabs: a remote agent tab opened', scope: '#hcn-p-tabs', must: ['#hcn-tabpanel', '[role="tab"][data-tab][aria-selected="true"]'], min: 3,
    open: async (page) => {
      await openSub(page, 'tabs');
      await page.locator('#hcn-tabs [role="tab"]').first().waitFor();
      const remote = page.locator('#hcn-tabs [role="tab"]', { hasText: /Dana/ }).first();
      await remote.click();
      await page.locator('#hcn-tabs [role="tab"][aria-selected="true"]', { hasText: /Dana/ }).waitFor();
    } },
  { id: 'links: linked workspaces', scope: '#hcn-p-links', must: ['#hcn-links li'], min: 1, open: async (page) => { await openSub(page, 'links'); await page.locator('#hcn-links li').first().waitFor(); } },
  { id: 'chat: composer with a menu of agents', scope: '#hcn-p-chat', must: ['#hcn-log'], min: 4, open: async (page) => { await openSub(page, 'chat'); await page.locator('#hcn-log').waitFor(); } },
];
