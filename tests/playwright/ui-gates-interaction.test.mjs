// T16 UI gates: the widgets that are operated, not just looked at, on the REAL page. A static audit proves a control has a name; these
// prove it behaves as the pattern it claims to be: a graph that is ONE Tab stop, diagram nodes that answer Enter and Space, a
// disclosure whose aria-expanded tells the truth, tabs wired to their panels, the expanded message editor as a real modal dialog,
// and the @-mention list as a real combobox/listbox.
//
// Run: node --test --test-concurrency=1 tests/playwright/ui-gates-interaction.test.mjs
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { launch, openReal, openSub, startRealServer } from './ui-gates-lib.mjs';
import { openMention } from './ui-gates-states.mjs';

let srv; let browser;
before(async () => { srv = await startRealServer({ mention_agent: true }); browser = await launch(); });
after(async () => { await browser?.close(); srv?.stop(); });
const session = (user = 'alice', opts = {}) => openReal(browser, srv.info, user, { reducedMotion: 'reduce', ...opts });

test('the lineage graph is ONE Tab stop (roving tabindex), and Tab leaves it', async () => {
  const { ctx, page } = await session();
  await openSub(page, 'lineage');
  await page.locator('svg.lin-svg g[data-node]').first().waitFor();
  const nodes = await page.locator('svg.lin-svg g[data-node]').count();
  assert.ok(nodes >= 8, `the seeded graph has several nodes (${nodes})`);
  const stops = () => page.locator('svg.lin-svg g[data-node][tabindex="0"]').count();
  assert.equal(await stops(), 1, `exactly one of the ${nodes} nodes is in the Tab order`);
  await page.locator('svg.lin-svg g[data-node][tabindex="0"]').focus();
  const first = await page.evaluate(() => document.activeElement.dataset.node);
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowRight');
  assert.equal(await stops(), 1, 'still one Tab stop after moving with the arrow keys');
  const moved = await page.evaluate(() => document.activeElement.dataset.node);
  assert.notEqual(moved, first, 'the arrow keys move between nodes');
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => !!document.activeElement.closest('svg.lin-svg')), false, 'one Tab leaves the graph');
  await ctx.close();
});

test('a node of the flow diagram answers Enter and answers Space', async () => {
  for (const key of ['Enter', 'Space']) {
    const { ctx, page } = await session();
    await openSub(page, 'flow');
    await page.locator('svg.cx-flow [data-node]').first().waitFor();
    const nodes = page.locator('svg.cx-flow [data-node]');
    assert.ok(await nodes.count() >= 2, 'the flow has people and agents');
    await nodes.nth(1).focus();
    const before = await page.locator('#hcn-flow-detail').innerText();
    await page.keyboard.press(key);
    await page.waitForFunction((b) => document.querySelector('#hcn-flow-detail').innerText !== b, before, { timeout: 5000 }).catch(() => assert.fail(`${key} on a flow node did nothing`));
    await ctx.close();
  }
});

test('the disclosure for acknowledged attention items says what it is: aria-expanded false, then true, then false', async () => {
  const { ctx, page } = await session();
  await openSub(page, 'attention');
  await page.locator('[data-attn-act="ack"]').first().click();
  const toggle = page.locator('[data-attn-act="toggle-quiet"]');
  await toggle.waitFor();
  assert.equal(await toggle.getAttribute('aria-expanded'), 'false', 'collapsed to begin with');
  const count = () => page.locator('#hcn-p-attention [data-attn-quiet], #hcn-p-attention .cx-attn-quiet').count();
  const closedItems = await page.locator('#hcn-p-attention .cx-attn-item').count();
  await toggle.click();
  await page.locator('[data-attn-act="toggle-quiet"][aria-expanded="true"]').waitFor({ timeout: 5000 }).catch(() => assert.fail('aria-expanded did not become true after the click'));
  const openItems = await page.locator('#hcn-p-attention .cx-attn-item').count();
  assert.ok(openItems > closedItems, `expanding shows the acknowledged items (${closedItems} -> ${openItems})`);
  await page.locator('[data-attn-act="toggle-quiet"]').click();
  await page.locator('[data-attn-act="toggle-quiet"][aria-expanded="false"]').waitFor({ timeout: 5000 }).catch(() => assert.fail('aria-expanded did not return to false'));
  assert.equal(await page.locator('#hcn-p-attention .cx-attn-item').count(), closedItems, 'and the items are hidden again');
  void count;
  await ctx.close();
});

test('every sub-tab controls a tabpanel that it labels, and its name is the label with the count spoken apart', async () => {
  const { ctx, page } = await session();
  await openSub(page, 'chat');
  const tabs = await page.locator('[role="tab"][data-sub]').evaluateAll((els) => els.map((b) => ({
    id: b.id, sub: b.dataset.sub, controls: b.getAttribute('aria-controls'), name: b.getAttribute('aria-label'), label: b.dataset.label,
    panel: (() => { const p = document.getElementById(b.getAttribute('aria-controls')); return p ? { role: p.getAttribute('role'), by: p.getAttribute('aria-labelledby') } : null; })() })));
  assert.ok(tabs.length >= 9, `the room has its sub-tabs (${tabs.length})`);
  for (const t of tabs) {
    assert.ok(t.panel, `${t.sub}: aria-controls="${t.controls}" points at a panel`);
    assert.equal(t.panel.role, 'tabpanel', `${t.sub}: the panel has role=tabpanel`);
    assert.equal(t.panel.by, t.id, `${t.sub}: the panel is labelled by its tab`);
    assert.match(t.name, new RegExp(`^${t.label}(, (\\d+|more than 99) waiting)?$`), `${t.sub}: the name is "${t.label}" with the count after a comma, never glued on ("${t.name}")`);
  }
  await ctx.close();
});

async function composerPage() {
  const { ctx, page } = await session();
  await openSub(page, 'chat');
  await page.locator('textarea.conversation-input').waitFor();
  return { ctx, page };
}

test('the expanded message editor is a modal dialog: role, name, Tab stays inside, Escape closes and gives focus back', async () => {
  const { ctx, page } = await composerPage();
  const expand = page.locator('[data-expand]');
  await expand.focus();
  await page.keyboard.press('Enter');
  const form = page.locator('form.conversation-composer--expanded');
  await form.waitFor();
  assert.equal(await form.getAttribute('role'), 'dialog');
  assert.equal(await form.getAttribute('aria-modal'), 'true');
  assert.ok((await form.getAttribute('aria-label')) || '', 'the dialog has a name');
  assert.equal(await expand.getAttribute('aria-expanded'), 'true');
  const inside = () => page.evaluate(() => { const a = document.activeElement; return !!a && !!a.closest('form.conversation-composer--expanded'); });
  assert.ok(await inside(), 'focus moved into the dialog when it opened');
  for (let i = 0; i < 14; i += 1) { await page.keyboard.press('Tab'); if (!(await inside())) assert.fail(`Tab number ${i + 1} left the dialog`); }
  for (let i = 0; i < 14; i += 1) { await page.keyboard.press('Shift+Tab'); if (!(await inside())) assert.fail(`Shift+Tab number ${i + 1} left the dialog`); }
  await page.locator('textarea.conversation-input').focus();
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('form.conversation-composer--expanded'));
  assert.equal(await page.locator('form.conversation-composer').first().getAttribute('role'), null, 'the dialog role is gone with the dialog');
  assert.equal(await page.evaluate(() => document.activeElement?.hasAttribute('data-expand')), true, 'focus returns to the Expand editor button');
  await ctx.close();
});

test('the @-mention list is a combobox with a listbox: aria-expanded, an active option that exists, arrow keys, Escape', async () => {
  const { ctx, page } = await composerPage();
  const input = page.locator('textarea.conversation-input');
  await openMention(page);
  const list = page.locator('ul[role="listbox"]:not([hidden])');
  assert.equal(await input.getAttribute('aria-expanded'), 'true');
  const options = list.locator('[role="option"]');
  assert.ok(await options.count() >= 1, 'the list offers someone to mention');
  const active = () => input.evaluate((el) => { const id = el.getAttribute('aria-activedescendant'); const o = id && document.getElementById(id); return o ? { id, selected: o.getAttribute('aria-selected'), role: o.getAttribute('role') } : null; });
  const a0 = await active();
  assert.ok(a0 && a0.role === 'option' && a0.selected === 'true', `aria-activedescendant names a selected option (${JSON.stringify(a0)})`);
  if (await options.count() > 1) { await page.keyboard.press('ArrowDown'); const a1 = await active(); assert.notEqual(a1.id, a0.id, 'ArrowDown moves the active option'); }
  await page.keyboard.press('Escape');
  await page.locator('ul[role="listbox"][hidden]').waitFor({ state: 'attached' });
  assert.equal(await input.getAttribute('aria-expanded'), 'false');
  assert.equal(await input.getAttribute('aria-activedescendant'), null, 'no active option once closed');
  await ctx.close();
});
