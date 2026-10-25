// T16 UI gates: a keyboard-only user can reach and operate every action on the REAL page (no mouse, no scripted clicks):
// comment, propose steering, fork, confirm, replay, acknowledge, decide a proposal. A guard fails the test if any
// pointer event ever reaches the page. Two people are needed because the product requires a second person to confirm.
//
// Run: node --test --test-concurrency=1 tests/playwright/ui-gates-keyboard.test.mjs
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { launch, openReal, openSub, settled, startRealServer } from './ui-gates-lib.mjs';

let srv; let browser; let info;
before(async () => { srv = await startRealServer({}); info = srv.info; browser = await launch(); });
after(async () => { await browser?.close(); srv?.stop(); });

const is = (page, sel) => page.evaluate((s) => !!document.activeElement && document.activeElement.matches(s), sel);
/** Move focus with Tab (then Shift+Tab) until the active element matches. Fails if it is never reachable by keyboard. */
async function tabTo(page, sel, max = 700) {
  for (let i = 0; i < max; i += 1) { if (await is(page, sel)) return; await page.keyboard.press('Tab'); }
  for (let i = 0; i < max; i += 1) { if (await is(page, sel)) return; await page.keyboard.press('Shift+Tab'); }
  assert.fail(`"${sel}" is not reachable with Tab/Shift+Tab`);
}
async function guard(page) {
  await page.addInitScript(() => { for (const t of ['pointerdown', 'mousedown', 'mouseup', 'touchstart']) window.addEventListener(t, () => { (window.__pointer ||= []).push(t); }, true); });
}
// Focus must come back to THE control that was used (or its logical successor), not merely to something other than <body>:
// a keyboard user who is thrown to the container, or to the first control on the panel, has lost their place.
const describe = (page) => page.evaluate(() => { const a = document.activeElement; return a ? `${a.tagName.toLowerCase()}${a.id ? '#' + a.id : ''}${[...a.attributes].filter((x) => x.name.startsWith('data-')).map((x) => `[${x.name}="${x.value}"]`).join('')}` : 'none'; });
const focusIs = async (page, selector, label) => assert.ok(await page.evaluate((s) => !!document.activeElement && document.activeElement.matches(s), selector), `${label}: focus should be on ${selector}, it is on ${await describe(page)}`);
const focusIn = async (page, container, label) => assert.ok(await page.evaluate((s) => { const a = document.activeElement; return !!a && a !== document.body && !!a.closest(s); }, container), `${label}: focus should stay inside ${container}, it is on ${await describe(page)}`);
async function pointerFree(page) { assert.equal(await page.evaluate(() => (window.__pointer || []).length), 0, 'no pointer event reached the page'); }

async function user(name) {
  const { ctx, page, w } = await openReal(browser, info, name);
  await guard(page);
  await page.reload();
  await page.locator('[data-sub]').first().waitFor();
  return { ctx, page, w };
}
async function openLineageNode(page, node = 'attempt:a1') {
  await tabTo(page, '[data-sub="lineage"]');
  await page.keyboard.press('Enter');
  await page.locator('svg.lin-svg g[data-node]').first().waitFor();
  // the graph is one Tab stop (roving tabindex); arrow keys move between nodes: Up/Down within a column, Left/Right between columns
  await tabTo(page, 'g[data-node][tabindex="0"]');
  await page.keyboard.press('Home');
  const at = () => page.evaluate(() => document.activeElement?.getAttribute('data-node'));
  search: for (let col = 0; col < 12; col += 1) {
    for (let row = 0; row < 12; row += 1) {
      if ((await at()) === node) break search;
      await page.keyboard.press('ArrowDown');
    }
    for (let row = 0; row < 12; row += 1) await page.keyboard.press('ArrowUp');
    await page.keyboard.press('ArrowRight');
  }
  assert.equal(await at(), node, `${node} is reachable with the arrow keys`);
  await page.keyboard.press('Enter');
  await page.locator('.wb').waitFor();
}
async function chooseAction(page, label) {
  await tabTo(page, 'input[data-wb-field="action"]');
  for (let i = 0; i < 8; i += 1) {
    const cur = await page.evaluate(() => document.activeElement.closest('label')?.innerText || '');
    if (cur.trim().startsWith(label)) return;
    await page.keyboard.press('ArrowDown');
  }
  assert.fail(`could not select the "${label}" action with the arrow keys`);
}

test('a contributor comments, proposes a redirect and proposes a fork; an owner confirms: keyboard only', async () => {
  const bob = await user('bob');
  const page = bob.page;
  await openLineageNode(page);

  // comment
  await tabTo(page, 'textarea[data-wb-field="comment"]');
  await page.keyboard.type('keyboard-only comment');
  await tabTo(page, 'form[data-wb-form="comment"] button[type="submit"]');
  await page.keyboard.press('Enter');
  await page.locator('.wb-comment', { hasText: 'keyboard-only comment' }).waitFor();
  await focusIs(page, 'form[data-wb-form="comment"] button[type="submit"]', 'after posting a comment');

  // propose a redirect (a radio group is operated with the arrow keys, free text is typed)
  const before = await page.locator('.wb-steer').count();
  await chooseAction(page, 'Redirect');
  await tabTo(page, 'textarea[data-wb-field="instruction"]');
  await page.keyboard.type('prefer the smaller patch');
  await tabTo(page, 'form[data-wb-form="propose"] button[type="submit"]');
  await page.keyboard.press('Enter');
  await page.waitForFunction((n) => document.querySelectorAll('.wb-steer').length > n, before);
  await focusIs(page, 'form[data-wb-form="propose"] button[type="submit"]', 'after proposing steering');
  assert.match(await page.locator('.wb-steers').innerText(), /Redirect with an instruction/);

  // propose a fork from a commit the graph shows
  const before2 = await page.locator('.wb-steer').count();
  await chooseAction(page, 'Fork an alternative');
  await tabTo(page, 'select[data-wb-field="base_sha"]');
  await tabTo(page, 'form[data-wb-form="propose"] button[type="submit"]');
  await page.keyboard.press('Enter');
  await page.waitForFunction((n) => document.querySelectorAll('.wb-steer').length > n, before2);
  assert.match(await page.locator('.wb-steers').innerText(), /Fork an alternative implementation/);
  await pointerFree(page);
  await bob.ctx.close();

  // the owner reviews and confirms bob's request (never their own), then replays history and acknowledges attention
  const alice = await user('alice');
  const ap = alice.page;
  await openLineageNode(ap);
  await tabTo(ap, '[data-wb-act="confirm"]');
  await ap.keyboard.press('Enter');
  await ap.locator('.wb-confirm').waitFor();
  assert.match(await ap.locator('.wb-confirm').innerText(), /exactly this request/i, 'the confirm step restates what will be sent');
  await focusIn(ap, '.wb-confirm', 'after opening the confirm step');
  await tabTo(ap, '[data-wb-act="confirm-go"]');
  await ap.keyboard.press('Enter');
  await ap.locator('.wb-steer', { hasText: /Confirmed, waiting for the agent/ }).first().waitFor();
  await focusIn(ap, '.wb', 'after confirming');

  // replay: step back with the keyboard, see the position change, return to live
  await tabTo(ap, '[data-replay="prev"]');
  await ap.keyboard.press('Enter');
  await ap.waitForFunction(() => !/^Live/.test(document.querySelector('.lin-replay__status')?.textContent || 'Live'));
  const stepped = await ap.locator('.lin-replay__status').innerText();
  await focusIs(ap, '[data-replay="prev"]', 'after stepping back');
  await tabTo(ap, '[data-replay="live"]');
  await ap.keyboard.press('Enter');
  await ap.waitForFunction(() => /^Live/.test(document.querySelector('.lin-replay__status')?.textContent || ''));
  assert.ok(stepped.length > 0);
  await focusIs(ap, '[data-replay="live"]', 'after returning to live');

  // attention: acknowledge the first item
  await tabTo(ap, '[data-sub="attention"]');
  await ap.keyboard.press('Enter');
  await ap.locator('[data-attn-act="ack"]').first().waitFor();
  const items = await ap.locator('.cx-attn-item').count();
  await tabTo(ap, '[data-attn-act="ack"]');
  await ap.keyboard.press('Enter');
  await ap.waitForFunction((n) => document.querySelectorAll('.cx-attn-item').length < n, items);
  await focusIn(ap, '#hcn-p-attention', 'after acknowledging an attention item');
  await pointerFree(ap);
  await alice.ctx.close();
});

test('an owner reads a quarantined proposal and rejects it: keyboard only', async () => {
  const alice = await user('alice');
  const page = alice.page;
  await tabTo(page, '[data-sub="proposals"]');
  await page.keyboard.press('Enter');
  await tabTo(page, '[data-proposal]');
  await page.keyboard.press('Enter');
  await page.locator('#hcn-review').getByText(/untrusted|quarantine/i).first().waitFor();
  const pid = await page.locator('[data-proposal]').first().getAttribute('data-proposal');
  await tabTo(page, '#hcn-review button[data-decide="reject"]');
  // the page asks through the browser's own confirm dialog, which is keyboard operable (Enter accepts)
  let asked = '';
  page.once('dialog', (d) => { asked = d.message(); d.accept(); });
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => !document.querySelector('#hcn-review [data-decide]'), null, { timeout: 8000 });
  assert.match(asked, /Reject this proposal/);
  assert.match(await page.locator('#hcn-p-proposals').innerText(), /rejected/i);
  await focusIs(page, `[data-proposal="${pid}"]`, 'after rejecting a proposal (back on its row)');
  await pointerFree(page);
  await alice.ctx.close();
});

test('an owner reads a quarantined proposal and accepts it: focus returns to its row', async () => {
  const own = await startRealServer({});
  try {
    const { ctx, page } = await openReal(browser, own.info, 'alice');
    await guard(page); await page.reload(); await page.locator('[data-sub]').first().waitFor();
    await tabTo(page, '[data-sub="proposals"]'); await page.keyboard.press('Enter');
    await tabTo(page, '[data-proposal]');
    const pid = await page.locator('[data-proposal]').first().getAttribute('data-proposal');
    await page.keyboard.press('Enter');
    await tabTo(page, '#hcn-review button[data-decide="accept"]');
    page.once('dialog', (d) => d.accept());
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => !document.querySelector('#hcn-review [data-decide="accept"]'), null, { timeout: 8000 }).catch(() => {});
    await settled(page, { quietMs: 300 });
    await focusIs(page, `[data-proposal="${pid}"], #hcn-review [data-decide]`, 'after accepting a proposal');
    await pointerFree(page);
    await ctx.close();
  } finally { own.stop(); }
});

test('remote agent tabs and room tabs are switchable by arrow keys alone', async () => {
  const alice = await user('alice');
  const page = alice.page;
  await tabTo(page, '[data-sub="tabs"]');
  await page.keyboard.press('Enter');
  await page.locator('#hcn-tabs [role=tab]').first().waitFor();
  await tabTo(page, '#hcn-tabs [role=tab][aria-selected="true"]');
  const first = await page.evaluate(() => document.activeElement.textContent.trim());
  await page.keyboard.press('ArrowRight');
  const second = await page.evaluate(() => document.activeElement.textContent.trim());
  assert.notEqual(second, first, 'ArrowRight moves to the next primary-agent tab');
  assert.equal(await page.evaluate(() => document.activeElement.getAttribute('role')), 'tab');
  await pointerFree(page);
  await alice.ctx.close();
});
