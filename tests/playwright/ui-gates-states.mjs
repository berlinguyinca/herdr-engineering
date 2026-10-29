// The UI states in which the controls actually live (T16 UI gates). A page-level audit of the default view of each tab never
// opens the inspector forms, the Gantt, the replay bar, the table view, the proposal review, the steering confirm step, the fork
// form, the consult/link forms, an expanded attention list or a remote tab, so it audited only a fraction of the controls.
// Each state below says how to reach it, which element must exist once it is open (a silent failure to open is a test failure,
// not a quiet skip) and the least number of controls and text nodes that must have been audited inside its scope. The floors are 90% of
// what the audit counted when they were set, so a state that renders half of itself, or a scope that slips, fails instead of passing on less.
//
// Used by ui-gates-states.test.mjs (accessibility) and ui-gates-xss.test.mjs (inert hostile text).
import { openSub, settled, scaled } from './ui-gates-lib.mjs';

/** Type "@" until the list opens: the room's agents arrive after the chat does, and a list asked for too early is simply empty. */
export async function openMention(page) {
  const input = page.locator('textarea.conversation-input');
  const opt = page.locator('ul[role="listbox"]:not([hidden]) [role="option"]').first();
  for (let i = 0; i < 20; i += 1) {
    await input.focus(); await input.fill(''); await page.keyboard.type('@');
    if (await opt.waitFor({ timeout: scaled(1500) }).then(() => true, () => false)) return;
  }
  throw new Error('the @-mention list never opened: the room has no agent that accepts tasks');
}
/** Wait for the room's agents (they arrive after the chat), then leave the composer empty and closed. */
/** The log starts as "No messages yet." and fills when the first poll answers: a state audited before that has two text nodes, not thirty. */
export const logLoaded = (page) => page.locator('#hcn-empty').waitFor({ state: 'detached' });
export async function chatReady(page) {
  // People can populate @ suggestions before the first room snapshot arrives;
  // the seeded conversation must also be present before auditing its editor.
  await logLoaded(page);
  await openMention(page);
  await page.keyboard.press('Escape');
  await page.locator('textarea.conversation-input').fill('');
  await page.locator('ul[role="listbox"][hidden]').waitFor({ state: 'attached' });
}
const nodeSelected = async (page, node = 'attempt:a1') => {
  await openSub(page, 'lineage');
  await page.locator('svg.lin-svg g[data-node]').first().waitFor();
  await page.locator(`g[data-node="${node}"]`).focus();
  await page.keyboard.press('Enter');
  await page.locator('.wb-comment').first().waitFor();
  await page.locator('form[data-ctx-form="link"]').waitFor();
  await settled(page);
};

/** @type {{id:string, scope:string, must:string[], min:number, open:(page:any)=>Promise<void>}[]} */
export const STATES = [
  { id: 'lineage: node selected (comment, steering, consult and link forms)', scope: '#hcn-p-lineage',
    // the selected node says so to assistive technology (aria-pressed true on it alone), not only in pixels
    must: ['form[data-wb-form="comment"]', 'form[data-wb-form="propose"]', 'form[data-ctx-form="ask"]', 'form[data-ctx-form="link"]',
      'g[data-node="attempt:a1"][aria-pressed="true"]', 'g[data-node][aria-pressed="false"]'], min: 56, minText: 170, open: nodeSelected },
  { id: 'lineage: an edited comment with its revision history', scope: '#hcn-p-lineage', must: ['[data-wb-act="history"]', '.wb-comment'], min: 59, minText: 184,
    open: async (page) => {
      await nodeSelected(page);
      await page.locator('textarea[data-wb-field="comment"]').fill('first wording');
      await page.locator('form[data-wb-form="comment"] button[type="submit"]').click();
      await page.locator('[data-wb-act="edit"]').first().waitFor();
      await page.locator('[data-wb-act="edit"]').first().click();
      await page.locator('textarea[data-wb-field="edit"]').fill('second wording');
      await page.locator('form[data-wb-form="edit"] button[type="submit"]').click();
      await page.locator('[data-wb-act="history"]').first().waitFor();
      await page.locator('[data-wb-act="history"]').first().click();
      await settled(page);
    } },
  { id: 'lineage: a retracted comment with its history', scope: '#hcn-p-lineage', must: ['[data-wb-act="history"]'], min: 60, minText: 180,
    open: async (page) => {
      await nodeSelected(page);
      page.on('dialog', (d) => d.accept());
      await page.locator('textarea[data-wb-field="comment"]').fill('to be taken back');
      await page.locator('form[data-wb-form="comment"] button[type="submit"]').click();
      await page.locator('[data-wb-act="retract"]').first().waitFor();
      await page.locator('[data-wb-act="retract"]').first().click();
      await page.locator('[data-wb-act="history"]').first().waitFor();
      await settled(page);
    } },
  { id: 'lineage: fork form (a base commit to fork from)', scope: '#hcn-p-lineage', must: ['select[data-wb-field="base_sha"]'], min: 62, minText: 182,
    open: async (page) => {
      await nodeSelected(page);
      await page.locator('input[data-wb-field="action"]').first().focus();
      for (let i = 0; i < 8 && !(await page.locator('select[data-wb-field="base_sha"]').count()); i += 1) await page.keyboard.press('ArrowDown');
    } },
  { id: 'lineage: steering confirm step (the request restated before it is sent)', scope: '#hcn-p-lineage', must: ['.wb-confirm', '[data-wb-act="confirm-go"]'], min: 61, minText: 182,
    open: async (page) => { await nodeSelected(page); await page.locator('[data-wb-act="confirm"]').first().click(); await page.locator('.wb-confirm').waitFor(); } },
  // collapsed by default on a phone (and still audited there, because the disclosure is opened by its own summary, as a person would)
  { id: 'lineage: filters disclosure opened', scope: '#hcn-p-lineage', must: ['details[data-filterbox][open] input[data-filter="q"]', 'details[data-filterbox][open] select[data-filter="state"]'], min: 20, minText: 20,
    open: async (page) => { await openSub(page, 'lineage'); await page.locator('svg.lin-svg g[data-node]').first().waitFor(); await page.evaluate(() => document.querySelector('details[data-filterbox]:not([open]) > summary')?.click()); await settled(page); } },
  { id: 'lineage: Gantt view', scope: '#hcn-p-lineage', must: ['svg.lin-svg[role="img"]', '[data-view="gantt"][aria-selected="true"]'], min: 25, minText: 171,
    open: async (page) => { await openSub(page, 'lineage'); await page.locator('svg.lin-svg g[data-node]').first().waitFor(); await page.locator('[data-view="gantt"]').click(); await page.locator('svg.lin-svg[role="img"]').waitFor(); await settled(page); } },
  { id: 'lineage: replay controls after stepping back', scope: '#hcn-p-lineage', must: ['[data-replay="slider"]', '[data-replay="live"]'], min: 30, minText: 68,
    open: async (page) => { await openSub(page, 'lineage'); await page.locator('svg.lin-svg g[data-node]').first().waitFor(); await page.locator('[data-replay="prev"]').click(); await page.waitForFunction(() => !/^Live/.test(document.querySelector('.lin-replay__status')?.textContent || 'Live')); } },
  { id: 'lineage: table alternative (a node selected, the live view)', scope: '#hcn-p-lineage',
    // the table marks the selected node's row (aria-current), and the Live button says it is the one in force (aria-pressed)
    must: ['table.lin-table', 'table.lin-table tr[aria-current="true"]', '[data-replay="live"][aria-pressed="true"]'], min: 60, minText: 240,
    open: async (page) => { await nodeSelected(page); await page.locator('#hcn-lineage [data-mode="table"]').first().click(); await page.locator('table.lin-table').first().waitFor(); } },
  { id: 'proposals: a quarantined proposal under review', scope: '#hcn-p-proposals', must: ['#hcn-review [data-decide="accept"]', '#hcn-review [data-decide="reject"]'], min: 18, minText: 36,
    open: async (page) => { await openSub(page, 'proposals'); await page.locator('[data-proposal]').first().click(); await page.locator('#hcn-review [data-decide]').first().waitFor(); } },
  { id: 'attention: acknowledged items expanded', scope: '#hcn-p-attention', must: ['[data-attn-act="toggle-quiet"][aria-expanded="true"]'], min: 4, minText: 17,
    open: async (page) => {
      await openSub(page, 'attention');
      await page.locator('[data-attn-act="ack"]').first().click();
      await page.locator('[data-attn-act="toggle-quiet"]').waitFor();
      await page.locator('[data-attn-act="toggle-quiet"]').click();
      await page.locator('[data-attn-act="toggle-quiet"][aria-expanded="true"]').waitFor();
    } },
  { id: 'tabs: a remote agent tab opened', scope: '#hcn-p-tabs', must: ['#hcn-tabpanel', '[role="tab"][data-tab][aria-selected="true"]'], min: 12, minText: 23,
    open: async (page) => {
      await openSub(page, 'tabs');
      await page.locator('#hcn-tabs [role="tab"]').first().waitFor();
      const remote = page.locator('#hcn-tabs [role="tab"]', { hasText: /Dana/ }).first();
      await remote.click();
      await page.locator('#hcn-tabs [role="tab"][aria-selected="true"]', { hasText: /Dana/ }).waitFor();
    } },
  { id: 'links: linked workspaces', scope: '#hcn-p-links', must: ['#hcn-links li'], min: 1, minText: 7, open: async (page) => { await openSub(page, 'links'); await page.locator('#hcn-links li').first().waitFor(); } },
  { id: 'chat: expanded message editor (a modal dialog)', scope: '#hcn-p-chat', must: ['form.conversation-composer--expanded[role="dialog"][aria-modal="true"]'], min: 14, minText: 24,
    open: async (page) => { await openSub(page, 'chat'); await chatReady(page); await page.locator('[data-expand]').click(); await page.locator('form.conversation-composer--expanded').waitFor(); await settled(page); } },
  { id: 'chat: the @-mention list open (a listbox of agents)', scope: '#hcn-p-chat', must: ['ul[role="listbox"]:not([hidden]) [role="option"]', 'textarea[role="combobox"][aria-expanded="true"]'], min: 15, minText: 25,
    open: async (page) => { await openSub(page, 'chat'); await openMention(page); } },
  { id: 'chat: composer with a menu of agents', scope: '#hcn-p-chat', must: ['#hcn-log[role="log"]'], min: 14, minText: 24, open: async (page) => { await openSub(page, 'chat'); await page.locator('#hcn-log').waitFor(); await logLoaded(page); await chatReady(page); } },
  // the other tabs' own views (round 3): a tab nobody audited is where a lost focus ring, a 1px-padded chip or a skipped heading hides
  { id: 'people: the members of the room', scope: '#hcn-p-people', must: ['#hcn-people li'], min: 4, minText: 12,
    open: async (page) => { await openSub(page, 'people'); await page.locator('#hcn-people li').first().waitFor(); } },
  { id: 'progress: evidence, a milestone and the analytics', scope: '#hcn-p-progress', must: ['[aria-label="Milestones"]', '#hcn-analytics table'], min: 0, minText: 28,
    // the fixture room links no mission, so the server sends no milestone; give the page one the way a linked mission would
    open: async (page) => {
      await page.route('**/api/v1/collab/**/progress*', async (route) => {
        const res = await route.fetch(); const body = await res.json();
        if (Array.isArray(body.milestones) && !body.milestones.length) body.milestones.push({ ...(body.room || {}), milestone: 'mission:m1', label: 'Milestone one', tasks: 1, complete: 0 });
        await route.fulfill({ response: res, json: body });
      });
      await openSub(page, 'progress'); await page.locator('[aria-label="Milestones"]').first().waitFor(); await page.locator('#hcn-analytics table').first().waitFor();
    } },
  { id: 'attention: the inbox', scope: '#hcn-p-attention', must: ['[aria-label="Items that need attention"]'], min: 2, minText: 8,
    open: async (page) => { await openSub(page, 'attention'); await page.locator('[aria-label="Items that need attention"]').first().waitFor(); } },
  { id: 'agents: the room agents', scope: '#hcn-p-agents', must: ['#hcn-p-agents [data-rename]', '#hcn-grant form[data-agent]', '#hcn-roomagents li'], min: 8, minText: 24,
    // "Your agents" is fetched after the room's agents: wait for it, or the number of controls depends on which answer came first
    open: async (page) => { await openSub(page, 'agents'); await page.locator('#hcn-p-agents [data-rename]').first().waitFor(); await page.locator('#hcn-grant form[data-agent]').first().waitFor(); await page.locator('#hcn-roomagents li').first().waitFor(); await settled(page); } },
  { id: 'flow: the diagram and its table', scope: '#hcn-p-flow', must: ['svg.cx-flow [data-node]'], min: 3, minText: 8,
    open: async (page) => { await openSub(page, 'flow'); await page.locator('svg.cx-flow [data-node]').first().waitFor(); } },
  { id: 'tasks: the task list', scope: '#hcn-p-tasks', must: ['#hcn-p-tasks li button'], min: 3, minText: 7,
    open: async (page) => { await openSub(page, 'tasks'); await page.locator('#hcn-p-tasks li button').first().waitFor(); await settled(page); } },
];

/** Run as the VIEWER (carol): the same panels with fewer controls. A viewer has no composer and cannot steer, and that must be audited too. */
export const VIEWER_STATES = [
  { id: 'viewer: chat', scope: '#hcn-p-chat', must: ['#hcn-log[role="log"]'], min: 0, minText: 10, open: async (page) => { await openSub(page, 'chat'); await page.locator('#hcn-log').waitFor(); await logLoaded(page); } },
  { id: 'viewer: lineage with a node selected', scope: '#hcn-p-lineage', must: ['.wb', 'svg.lin-svg g[data-node]'], min: 37, minText: 147,
    open: async (page) => {
      await openSub(page, 'lineage');
      await page.locator('svg.lin-svg g[data-node]').first().waitFor();
      await page.evaluate(() => document.querySelector('details[data-filterbox]:not([open]) > summary')?.click());   // collapsed on a phone: open it so its controls are audited
      await page.locator('g[data-node="attempt:a1"]').focus(); await page.keyboard.press('Enter');
      await page.locator('.wb').waitFor(); await settled(page);
    } },
];
