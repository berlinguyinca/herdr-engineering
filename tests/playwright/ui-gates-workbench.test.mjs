// Mission Workbench gates (S5), on the REAL assembled page with the `workbench` fixture: a Pi agent that rolled over through 12 sessions,
// forked into a Claude branch and a Pi continuation, with a PR, CI, reviews, artifacts, a blocked task and a merge gate.
//   * the inspector shows only measured facts and says "not reported" otherwise; an artifact never shows where its bytes live;
//   * pan / zoom / focus / fit work by pointer, wheel with a modifier, and keyboard; live vs replay is a word, not a colour;
//   * the DOM stays bounded for a 64-session chain and a 1500-node graph;
//   * accessibility (static, focus, motion), 44px targets, and reflow at 375 and 320.
// Run: node --test --test-concurrency=1 tests/playwright/ui-gates-workbench.test.mjs
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { launch, openReal, openSub, settled, startRealServer } from './ui-gates-lib.mjs';
import { focusIndicatorAudit, motionAudit, overflowAudit, overflowFailure, staticAudit, summarize } from './a11y-checks.mjs';

let srv; let big; let long; let browser;
before(async () => {
  srv = await startRealServer({ fixed_clock: true, empty: true, workbench: true });
  browser = await launch();
});
after(async () => { await browser?.close(); srv?.stop(); big?.stop(); long?.stop(); });

const open = async (info = srv.info, opts = {}) => {
  const s = await openReal(browser, info, 'alice', { reducedMotion: 'reduce', ...opts });
  await openSub(s.page, 'lineage');
  await s.page.locator('svg.lin-svg g[data-node]').first().waitFor({ timeout: 30000 });
  await settled(s.page);
  return s;
};
const drawn = (page) => page.locator('[data-viewport] svg.lin-svg g[data-node]').count();
const zoom = async (page) => Number((await page.locator('.lin-zoom').innerText()).replace('%', ''));
const select = async (page, id) => {
  const n = page.locator(`g[data-node="${id}"]`);
  if (!(await n.count())) {                    // not in the current view: find it with the search box, as a person would
    await page.evaluate(() => { const d = document.querySelector('details[data-filterbox]'); if (d && !d.open) d.open = true; });   // collapsed on a phone
    await page.locator('input[data-filter="q"]').fill(id.split(':').slice(1).join(':'));
    await n.waitFor({ timeout: 10000 });
  }
  await n.focus();
  await page.keyboard.press('Enter');
  if (await page.locator('input[data-filter="q"]').inputValue().catch(() => '')) await page.locator('input[data-filter="q"]').fill('');
  await page.locator('.lin-inspector').waitFor();
  await page.locator(`.lin-inspector[aria-label="Inspector for ${(await page.locator('.lin-inspector h3').first().innerText())}"]`).waitFor();
  await settled(page);
};
const inspector = (page) => page.locator('aside[aria-label="Inspector"] .lin-inspector').innerText();

test('the default workbench is the status strip, the graph with its time controls, and the inspector; advanced views stay behind tabs and disclosures', async () => {
  const { ctx, page } = await open();
  assert.ok(await page.locator('.lin-status').isVisible());
  assert.match(await page.locator('.lin-status').innerText(), /Needs attention[\s\S]*Fix parser fixture[\s\S]*Approvals waiting[\s\S]*Merge approval/);
  assert.equal(await page.locator('[data-view="graph"][aria-selected="true"]').count(), 1);
  assert.match(await page.locator('[data-mode-badge]').first().innerText(), /Live/);
  assert.equal(await page.locator('details.lin-legend').first().getAttribute('open'), null, 'the legend is collapsed');
  assert.equal(await page.locator('svg.lin-svg[role=img]').count(), 0, 'the Gantt is a tab away');
  await ctx.close();
});

test('inspector: a session shows its handoff from and to, checkpoint sha and cause; nothing is invented', async () => {
  const { ctx, page } = await open();
  await page.locator('[data-vp="end"]').click();
  await select(page, 'session:pi-12');
  const t = await inspector(page);
  assert.match(t, /Received from[\s\S]*Pi session 11[\s\S]*checkpoint cccccccccccc…[\s\S]*cause context_threshold/);
  assert.match(t, /Handed on to[\s\S]*Pi session 13[\s\S]*checkpoint cccccccccccc…[\s\S]*cause phase_boundary/);
  assert.match(t, /Mission\s+Parser rewrite mission: ● running/);
  assert.match(t, /Artifacts[\s\S]*Checkpoint 11[\s\S]*checkpoint · version #11 · digest bbbbbbbbbbbb…/);
  assert.match(t, /Next action\s+not reported/, 'a session reports no next action');
  assert.ok(!/\d+\s*%/.test(t), 'no percentage is shown');
  assert.match(t, /This page offers no terminal and no commands/, 'the standalone page offers no terminal');
  await ctx.close();
});

test('inspector: blockers and next action of a blocked task, PR / CI / review of a PR, and no storage location of an artifact', async () => {
  const { ctx, page } = await open();
  await select(page, 'task:fix-ci');
  let t = await inspector(page);
  assert.match(t, /Blockers\s+CI run 117 blocked by, failed/);
  assert.match(t, /CI run 117 failed on the parser fixture/);
  assert.match(t, /Next action\s+Rerun CI after the fixture fix/);
  await select(page, 'pr:github.com/acme/parser#42');
  t = await inspector(page);
  assert.match(t, /Pull request\s+PR 42: parser rewrite[\s\S]*head <?b+/);
  assert.match(t, /CI\s+CI run 118[\s\S]*passed/);
  assert.match(t, /Review\s+Independent review/);
  assert.match(t, /stale/, 'the review bound to the older head is marked stale');
  await select(page, 'artifact:report-parser');
  t = await inspector(page);
  assert.match(t, /version v2 · digest sha256:5e5e5…/);
  assert.match(t, /Storage locations are not shown/);
  const body = await page.locator('body').innerText();
  for (const leak of ['s3://', 'private-bucket', 'blob.invalid']) assert.ok(!body.includes(leak), `${leak} is nowhere on the page`);
  const html = await page.content();
  for (const leak of ['s3://', 'private-bucket', 'blob.invalid']) assert.ok(!html.includes(leak), `${leak} is nowhere in the DOM`);
  await ctx.close();
});

test('graph: nodes carry kind chips and links read handoff, branch, review and dependency', async () => {
  const { ctx, page } = await open();
  const chips = await page.locator('.lin-chip text').evaluateAll((els) => els.map((e) => e.textContent));
  for (const c of ['AGT', 'ATT', 'SES']) assert.ok(chips.includes(c), `chip ${c} is drawn`);
  await page.locator('[data-vp="end"]').click();
  await settled(page);
  const tags = new Set(await page.locator('.lin-edge-tag').evaluateAll((els) => els.map((e) => e.textContent)));
  for (const w of ['handoff', 'branch']) assert.ok(tags.has(w), `link word ${w} (drawn: ${[...tags].join(', ')})`);
  await ctx.close();
});

test('pan and zoom: buttons, wheel with a modifier (and not without), drag, fit; the drawing stays inside its cap', async () => {
  const { ctx, page } = await open();
  const z0 = await zoom(page);
  await page.locator('[data-vp="in"]').click();
  assert.ok(await zoom(page) > z0, 'Zoom in raises the zoom');
  await page.locator('[data-vp="out"]').click(); await page.locator('[data-vp="out"]').click();
  assert.ok(await zoom(page) < z0, 'Zoom out lowers it');
  await page.locator('[data-vp="fit"]').click();
  assert.equal(await zoom(page), z0, 'Fit returns to the starting view');
  const box = await page.locator('[data-viewport]').boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.wheel(0, -300);
  await settled(page);
  assert.equal(await zoom(page), z0, 'a plain wheel does not zoom (it scrolls the page)');
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.locator('[data-viewport]').scrollIntoViewIfNeeded();
  await page.locator('[data-viewport]').scrollIntoViewIfNeeded();
  const box2 = await page.locator('[data-viewport]').boundingBox();
  await page.mouse.move(box2.x + box2.width / 2, box2.y + box2.height / 2);
  await page.keyboard.down('Control'); await page.mouse.wheel(0, -300); await page.keyboard.up('Control');
  await page.waitForFunction((z) => Number(document.querySelector('.lin-zoom').innerText.replace('%', '')) > z, z0);
  await page.locator('[data-vp="fit"]').click();
  await page.locator('[data-viewport]').scrollIntoViewIfNeeded();
  const box3 = await page.locator('[data-viewport]').boundingBox();
  const vb0 = await page.locator('[data-viewport] svg.lin-svg').getAttribute('viewBox');
  await page.mouse.move(box3.x + 40, box3.y + box3.height - 12);
  await page.mouse.down(); await page.mouse.move(box3.x + 140, box3.y + box3.height - 12, { steps: 6 }); await page.mouse.up();
  await settled(page);
  assert.notEqual(await page.locator('[data-viewport] svg.lin-svg').getAttribute('viewBox'), vb0, 'dragging the background pans');
  assert.ok(await drawn(page) <= 242);
  await ctx.close();
});

test('keyboard: + - 0 and Shift+arrows act on the graph; an arrow to a node out of view brings it into view', async () => {
  const { ctx, page } = await open();
  await page.locator('[data-viewport]').focus();
  const z0 = await zoom(page);
  await page.keyboard.press('+'); assert.ok(await zoom(page) > z0);
  await page.keyboard.press('-'); await page.keyboard.press('-'); assert.ok(await zoom(page) < z0);
  await page.keyboard.press('0'); assert.equal(await zoom(page), z0);
  const vb0 = await page.locator('[data-viewport] svg.lin-svg').getAttribute('viewBox');
  await page.keyboard.press('ArrowRight');
  assert.notEqual(await page.locator('[data-viewport] svg.lin-svg').getAttribute('viewBox'), vb0, 'an arrow on the graph itself pans');
  await page.keyboard.press('0');
  // walk right along the nodes until the focused one was out of the first view; it must be on screen
  await page.locator('g[data-node][tabindex="0"]').focus();
  for (let i = 0; i < 14; i += 1) await page.keyboard.press('ArrowRight');
  const inView = await page.evaluate(() => {
    const el = document.activeElement; const v = document.querySelector('[data-viewport]');
    if (!el || !el.dataset || !el.dataset.node) return null;
    const a = el.getBoundingClientRect(); const b = v.getBoundingClientRect();
    return { node: el.dataset.node, inside: a.right > b.left && a.left < b.right && a.bottom > b.top && a.top < b.bottom };
  });
  assert.ok(inView && inView.inside, `focused node ${inView && inView.node} is in the viewport`);
  assert.equal(await page.locator('g[data-node][tabindex="0"]').count(), 1, 'still a single Tab stop');
  await ctx.close();
});

test('focus on a lineage keeps only its ancestors and descendants, and Show all restores the graph', async () => {
  const { ctx, page } = await open();
  assert.ok(await page.locator('[data-vp="focus"]').isDisabled(), 'nothing selected, nothing to focus');
  const shown = async () => Number((await page.locator('#hcn-lineage').innerText()).match(/(\d+) nodes, \d+ links shown/)[1]);
  const all = await shown();
  await page.locator('[data-vp="end"]').click();
  await select(page, 'session:claude-1');
  await page.locator('[data-vp="focus"]').click();
  await page.locator('[data-vp="unfocus"]').waitFor();
  const names = await page.locator('g[data-node]').evaluateAll((els) => els.map((e) => e.dataset.node));
  assert.ok(names.includes('attempt:claude-1') && names.includes('session:claude-1'), `the selected node is in view after focusing (${names.length} drawn)`);
  assert.ok(!names.includes('session:pi-13'), 'the sibling branch is not part of the Claude lineage');
  assert.ok(await shown() < all, `${await shown()} of ${all} nodes remain`);
  assert.equal(await page.locator('g[data-node="session:claude-1"][aria-pressed="true"]').count(), 1, 'the selection survives');
  await page.locator('[data-vp="unfocus"]').click();
  await page.locator('[data-vp="focus"]').waitFor();
  assert.equal(await shown(), all);
  await ctx.close();
});

test('live and replay are different words in the badge, and the graph at an earlier position says Replay', async () => {
  const { ctx, page } = await open();
  assert.equal(await page.locator('[data-mode-badge]').first().getAttribute('data-mode-badge'), 'live');
  await page.locator('[data-replay="prev"]').click();
  await page.locator('[data-mode-badge="replay"]').waitFor({ timeout: 15000 });
  assert.match(await page.locator('[data-mode-badge="replay"]').innerText(), /Replay: event \d+ of \d+/);
  await page.locator('[data-replay="live"]').click();
  await page.locator('[data-mode-badge="live"]').waitFor({ timeout: 15000 });
  await ctx.close();
});

test('the selected node is marked in the Gantt', async () => {
  const { ctx, page } = await open();
  await select(page, 'attempt:wb1');
  await page.locator('[data-view="gantt"]').click();
  await page.locator('svg.lin-svg[role=img]').waitFor();
  assert.equal(await page.locator('g[data-lane="attempt:wb1"][data-selected="1"]').count(), 1);
  await ctx.close();
});

test('layout preferences are remembered in this browser only (table mode), not sent to the server', async () => {
  const { ctx, page } = await open();
  const posts = [];
  page.on('request', (r) => { if (r.method() !== 'GET') posts.push(r.url()); });
  await page.locator('[data-mode="table"]').first().click();
  await page.locator('table.lin-table').first().waitFor();
  await page.reload();
  await openSub(page, 'lineage');
  await page.locator('table.lin-table').first().waitFor({ timeout: 15000 });
  assert.deepEqual(posts.filter((u) => /prefs|layout|lineage/.test(u)), [], 'nothing about the layout was written to the server');
  await ctx.close();
});

// ---- accessibility ---------------------------------------------------------------------------------------------------
for (const scheme of ['light', 'dark']) {
  test(`a11y (${scheme}): the workbench with a selected node passes the static, focus, motion and reflow audits`, async () => {
    const { ctx, page } = await open(srv.info, { colorScheme: scheme });
    await page.locator('[data-vp="end"]').click();
    await select(page, 'session:pi-12');
    const f = await staticAudit(page, '#hcn-p-lineage');
    assert.deepEqual(f, [], summarize(f));
    assert.ok(f.stats.controls > 40, `the audit covered ${f.stats.controls} controls`);
    const fi = await focusIndicatorAudit(page, '#hcn-p-lineage');
    assert.deepEqual(fi, [], summarize(fi));
    assert.deepEqual(await motionAudit(page), []);
    assert.equal(overflowFailure(await overflowAudit(page)), '');
    await ctx.close();
  });
}

for (const [w, h] of [[375, 667], [320, 640]]) {
  test(`mobile ${w}px: status and attention come first, targets are 44px, nothing scrolls sideways, the graph is usable`, async () => {
    const { ctx, page } = await open(srv.info, { viewport: { width: w, height: h } });
    const strip = await page.locator('.lin-status').boundingBox();
    const graph = await page.locator('[data-viewport]').boundingBox();
    const filters = await page.locator('details[data-filterbox]').boundingBox();
    assert.ok(strip.y < filters.y && strip.y < graph.y, 'the status strip is above the filters and the graph');
    const attn = await page.locator('.lin-status .lin-status__need').first().boundingBox();
    assert.ok(attn.y + attn.height < h * 2.2, 'the attention list is within the first two screens');
    for (const sel of ['[data-vp="in"]', '[data-vp="out"]', '[data-vp="fit"]', '[data-vp="end"]']) {
      const b = await page.locator(sel).boundingBox();
      assert.ok(b.width >= 44 && b.height >= 44, `${sel} is ${b.width}x${b.height}`);
    }
    assert.equal(overflowFailure(await overflowAudit(page)), '');
    const g = await page.locator('[data-viewport]').boundingBox();
    assert.ok(g.width <= w && g.width >= w - 80, `the graph fits the screen (${g.width}px of ${w})`);
    await page.locator('[data-vp="in"]').click();
    await select(page, (await page.locator('g[data-node]').first().getAttribute('data-node')));
    const f = await staticAudit(page, '#hcn-p-lineage');
    assert.deepEqual(f, [], summarize(f));
    await ctx.close();
  });
}

// ---- filters disclosure and minimap (S5 follow-ups) ---------------------------------------------------------------------
test('filters: open on a wide page, collapsed on a phone, operable by keyboard, and a collapsed box still says how many are active', async () => {
  const wide = await open();
  assert.equal(await wide.page.locator('details[data-filterbox]').evaluate((d) => d.open), true, 'open at 1280px');
  assert.ok(await wide.page.locator('input[data-filter="q"]').isVisible());
  await wide.ctx.close();
  const { ctx, page } = await open(srv.info, { viewport: { width: 375, height: 700 } });
  const box = page.locator('details[data-filterbox]');
  assert.equal(await box.evaluate((d) => d.open), false, 'collapsed at 375px');
  assert.ok(!(await page.locator('input[data-filter="q"]').isVisible()), 'the search box is not on screen until the box is opened');
  const sum = page.locator('details[data-filterbox] > summary');
  const b = await sum.boundingBox();
  assert.ok(b.height >= 44 && b.width >= 44, `the summary is a ${b.width}x${b.height} target`);
  for (const audit of [await staticAudit(page, '#hcn-p-lineage')]) assert.deepEqual(audit, [], summarize(audit));
  await sum.focus();
  await page.keyboard.press('Enter');
  assert.equal(await box.evaluate((d) => d.open), true, 'Enter opens it');
  await page.locator('input[data-filter="q"]').fill('session');
  await settled(page);
  assert.equal(await box.evaluate((d) => d.open), true, 'a redraw keeps the box as the person left it');
  await sum.focus();
  await page.keyboard.press('Space');
  assert.equal(await box.evaluate((d) => d.open), false, 'Space closes it');
  assert.match(await sum.innerText(), /Filters\s*\(1 active\)/, 'the active filter is still announced when collapsed');
  assert.ok(await page.locator('g[data-node]').count() > 0, 'the filter applies while collapsed');
  await sum.focus(); await page.keyboard.press('Enter');
  const f = await staticAudit(page, '#hcn-p-lineage');
  assert.deepEqual(f, [], summarize(f));
  assert.equal(overflowFailure(await overflowAudit(page)), '');
  await ctx.close();
});

test('minimap: a text alternative that follows the view, no focus stop, no motion, click moves the view', async () => {
  const { ctx, page } = await open();
  const mm = page.locator('svg.lin-mm');
  assert.equal(await mm.count(), 1, 'the workbench graph is wider than the viewport');
  assert.equal(await mm.getAttribute('role'), 'img');
  const before = await mm.getAttribute('aria-label');
  assert.match(before, /^Overview map: the view shows columns 1 to \d+ of \d+ and rows 1 to \d+ of \d+\./);
  assert.equal(await page.locator('[data-minimap] :is(a, button, input, [tabindex])').count(), 0, 'nothing inside the minimap can take focus');
  assert.equal(await page.locator('[data-minimap]').evaluate((el) => el.matches(':focus-within') || el.tabIndex >= 0), false);
  // keyboard: Tab leaves the toolbar through the viewport and onward; it never gets stuck on or inside the map
  await page.locator('[data-vp="end"]').focus();
  const seen = [];
  for (let i = 0; i < 6; i += 1) { await page.keyboard.press('Tab'); seen.push(await page.evaluate(() => `${document.activeElement.tagName}:${document.activeElement.closest('[data-minimap]') ? 'IN-MAP' : ''}${document.activeElement.getAttribute('data-vp') || document.activeElement.className.toString().slice(0, 20)}`)); }
  assert.ok(!seen.some((x) => x.includes('IN-MAP')), seen.join(' | '));
  assert.ok(new Set(seen).size >= 2, `focus keeps moving: ${seen.join(' | ')}`);
  const vb0 = await page.locator('svg.lin-svg').getAttribute('viewBox');
  await mm.scrollIntoViewIfNeeded();
  const r = await mm.boundingBox();
  await page.mouse.click(r.x + r.width * 0.9, r.y + r.height * 0.5);
  await settled(page);
  assert.notEqual(await page.locator('svg.lin-svg').getAttribute('viewBox'), vb0, 'a click on the map moved the view');
  assert.notEqual(await page.locator('svg.lin-mm').getAttribute('aria-label'), before, 'the text alternative follows the view');
  assert.ok(await drawn(page) <= 242);
  const motion = await page.evaluate(() => [...document.querySelectorAll('.lin-minimap, .lin-mm, .lin-mm-view, .lin-mm-bar')].map((el) => getComputedStyle(el)).map((c) => `${c.transitionDuration}/${c.animationName}`));
  assert.ok(motion.every((m) => /^(0s|1e-06s)\/none$/.test(m)), `no transition or animation (reduced motion clamps to 1e-06s): ${[...new Set(motion)].join(",")}`);
  await ctx.close();
});

test('terminal authorization is not offered on the standalone page, whatever the node', async () => {
  const { ctx, page } = await open();
  const posts = [];
  page.on('request', (r) => { if (/\/authorize/.test(r.url())) posts.push(r.url()); });
  await page.locator('[data-vp="end"]').click();
  await select(page, 'session:pi-12');
  assert.equal(await page.locator('[data-term-auth]').count(), 0);
  assert.deepEqual(posts, []);
  await ctx.close();
});

// ---- scale -----------------------------------------------------------------------------------------------------------
test('a 64-session chain: the drawing is bounded, the newest session is one key away, and its handoff is inspectable', async () => {
  long = await startRealServer({ fixed_clock: true, empty: true, workbench: true, chain: 64 });
  const { ctx, page } = await open(long.info);
  assert.ok(await drawn(page) <= 242);
  await page.locator('[data-vp="end"]').click();
  await settled(page);
  assert.ok(await drawn(page) <= 242);
  await select(page, 'session:pi-64');
  assert.match(await inspector(page), /Received from[\s\S]*Pi session 63/);
  // End/Home move through the nodes in reading order, and the newest is reachable by keyboard alone
  await page.locator('g[data-node][tabindex="0"]').focus();
  await page.keyboard.press('Home');
  await page.keyboard.press('End');
  assert.ok(await page.evaluate(() => { const a = document.activeElement.getBoundingClientRect(); const b = document.querySelector('[data-viewport]').getBoundingClientRect(); return a.right > b.left && a.left < b.right; }), 'End brings the last node into view');
  await ctx.close();
});

test('a graph of 1500 nodes: the DOM stays bounded at every zoom, pan and zoom stay responsive, and the table lists the rest', async () => {
  big = await startRealServer({ fixed_clock: true, graph_nodes: 1500, timeout: 300000 });
  const { ctx, page } = await open(big.info);
  for (let i = 0; i < 12; i += 1) {
    const before = await page.locator('#hcn-lineage').innerText();
    if (!(await page.evaluate(() => { const b = document.querySelector('[data-more]'); if (b) b.click(); return Boolean(b); }))) break;
    await page.waitForFunction((b) => document.querySelector('#hcn-lineage')?.innerText !== b, before, { timeout: 60000 });
  }
  const loaded = Number((await page.locator('#hcn-lineage').innerText()).match(/(\d+) nodes, \d+ links shown/)?.[1] || 0);
  assert.ok(loaded >= 1000, `${loaded} nodes loaded`);
  const bound = async (label) => {
    const n = await drawn(page);
    const els = await page.locator('[data-viewport] *').count();
    assert.ok(n >= 1 && n <= 242, `${label}: ${n} nodes drawn`);
    assert.ok(els < 4000, `${label}: ${els} elements inside the viewport`);
  };
  await bound('fit');
  const t0 = Date.now();
  for (let i = 0; i < 4; i += 1) await page.locator('[data-vp="out"]').click();
  await bound('zoomed out');
  for (let i = 0; i < 8; i += 1) await page.locator('[data-vp="in"]').click();
  await bound('zoomed in');
  await page.locator('[data-viewport]').focus();
  for (let i = 0; i < 10; i += 1) await page.keyboard.press('Shift+ArrowRight');
  await bound('panned');
  assert.ok(Date.now() - t0 < 30000, `22 view changes took ${Date.now() - t0} ms`);
  await page.locator('[data-mode="table"]').first().click();
  await page.locator('table.lin-table').first().waitFor();
  assert.ok(await page.locator('table.lin-table tbody tr').count() > 1000, 'the accessible table lists the nodes');
  await ctx.close();
});
