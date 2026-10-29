// T16 UI gates: the widgets that are operated, not just looked at, on the REAL page. A static audit proves a control has a name; these
// prove it behaves as the pattern it claims to be: a graph that is ONE Tab stop, diagram nodes that answer Enter and Space, a
// disclosure whose aria-expanded tells the truth, tabs wired to their panels, the expanded message editor as a real modal dialog,
// and the @-mention list as a real combobox/listbox.
//
// Run: node --test --test-concurrency=1 tests/playwright/ui-gates-interaction.test.mjs
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { launch, openReal, openSub, startRealServer, scaled } from './ui-gates-lib.mjs';
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
    await page.waitForFunction((b) => document.querySelector('#hcn-flow-detail').innerText !== b, before, { timeout: scaled(5000) }).catch(() => assert.fail(`${key} on a flow node did nothing`));
    await ctx.close();
  }
});

test('a flow redraw keeps keyboard focus on the remembered node instead of dropping it to <body> (WCAG 2.4.3)', async () => {
  const { ctx, page } = await session();
  await openSub(page, 'flow');
  await page.locator('svg.cx-flow [data-node]').first().waitFor();
  const nodes = page.locator('svg.cx-flow [data-node]');
  assert.ok(await nodes.count() >= 2, 'the flow has people and agents');
  await nodes.first().focus();
  await page.keyboard.press('ArrowRight');           // the roving stop moves off the first item
  const stop = await page.evaluate(() => document.activeElement.dataset.node || document.activeElement.dataset.edge);
  assert.ok(stop, 'focus is on an item of the diagram');
  // New flow data arrives: redraw through the page's own collab-flow.js module (the same instance the page uses), with every
  // current node and one new task line, exactly as drawFlow does when a poll changes the flow.
  const after = await page.evaluate(async () => {
    const url = performance.getEntriesByType('resource').map((e) => e.name).find((n) => /\/collab-flow\.js(\?|$)/.test(n));
    const { renderFlow } = await import(url);
    const box = document.getElementById('hcn-flow');
    const ids = [...box.querySelectorAll('[data-node]')].map((el) => el.dataset.node);
    const flow = { nodes: ids.map((id) => ({ id, kind: 'agent', name: id })), edges: [{ from: ids[0], to: ids[1], counts: { pending: 1 }, last: 'now' }], tasks: [] };
    renderFlow(box, flow, {});
    const a = document.activeElement;
    return { inBox: box.contains(a), tag: a.tagName, id: a.dataset?.node || a.dataset?.edge || null, tabindex: a.getAttribute?.('tabindex') };
  });
  assert.equal(after.inBox, true, `focus fell out of the diagram to <${after.tag}>`);
  assert.equal(after.tabindex, '0', 'the focused element is the one Tab stop');
  // the remembered node exists in the new data, so focus returns to it (an edge key changes with the data, then the first item is right)
  if (await page.locator(`svg.cx-flow [data-node="${stop}"]`).count()) assert.equal(after.id, stop, 'focus returns to the remembered node');
  await ctx.close();
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
  await page.locator('[data-attn-act="toggle-quiet"][aria-expanded="true"]').waitFor({ timeout: scaled(5000) }).catch(() => assert.fail('aria-expanded did not become true after the click'));
  const openItems = await page.locator('#hcn-p-attention .cx-attn-item').count();
  assert.ok(openItems > closedItems, `expanding shows the acknowledged items (${closedItems} -> ${openItems})`);
  await page.locator('[data-attn-act="toggle-quiet"]').click();
  await page.locator('[data-attn-act="toggle-quiet"][aria-expanded="false"]').waitFor({ timeout: scaled(5000) }).catch(() => assert.fail('aria-expanded did not return to false'));
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
  // The trap is at the ends. Start from the last control that is not the textarea (whose own Tab handler indents) and require Tab to
  // wrap to the first, then from the first and require Shift+Tab to wrap to the last. A form whose edge lets focus escape fails here.
  const ring = await form.evaluate((f) => [...f.querySelectorAll('button, input, textarea, select, [tabindex="0"]')].filter((e) => !e.disabled && e.getClientRects().length && e.tabIndex >= 0).map((e, i) => { e.dataset.ring = String(i); return { i, tag: e.tagName }; }));
  assert.ok(ring.length >= 4, `the dialog has several controls (${ring.length})`);
  const first = ring[0].i; const last = ring.at(-1).i;
  const at = () => page.evaluate(() => document.activeElement?.dataset.ring ?? null);
  if (ring.at(-1).tag !== 'TEXTAREA') {
    await page.locator(`[data-ring="${last}"]`).focus(); await page.keyboard.press('Tab');
    assert.equal(await at(), String(first), 'Tab from the last control wraps to the first, inside the dialog');
  }
  if (ring[0].tag !== 'TEXTAREA') {
    await page.locator(`[data-ring="${first}"]`).focus(); await page.keyboard.press('Shift+Tab');
    assert.equal(await at(), String(last), 'Shift+Tab from the first control wraps to the last, inside the dialog');
  }
  assert.ok(await inside(), 'focus never left the dialog');
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
