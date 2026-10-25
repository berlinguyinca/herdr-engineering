// T16 UI gates: hostile text is INERT on every surface, not just the four a single hand-seeded string reaches.
//
// A server may validate what it accepts (workspace ids are limited to [A-Za-z0-9._:-], handles to [a-z0-9-]), but the page must
// not rely on that: a renderer that forgets to escape is one bug away from script execution. So every response the page reads from
// /api/v1/collab/ passes through a rewriting proxy that prefixes a canary payload to every field a person or an agent can fill
// with text (names, labels, titles, instructions, reasons, activity, workspace ids, hosts, evidence, ...) and swaps every url
// for a javascript: URL. The page is then driven through every surface AND every state (inspector forms, confirm step, Gantt,
// proposal review, remote tab, ...) and must show the payload as text, create no element or handler from it, and execute nothing.
//
// A field that never reaches the screen would make a mutant that stops escaping it undetectable, so the test also keeps score:
// the fields whose canary was seen on a page, per surface, are listed in the output and a floor of them is required.
//
// Run: node --test --test-concurrency=1 tests/playwright/ui-gates-xss.test.mjs
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { launch, openReal, openSub, settled, startRealServer } from './ui-gates-lib.mjs';
import { STATES } from './ui-gates-states.mjs';
import { URLS, UNREAD, tag, taint } from './ui-gates-taint.mjs';

const allUsed = new Set();      // every field the proxy tainted, over all tests
let srv; let browser;
before(async () => { srv = await startRealServer({ rich: true, mention_agent: true }); browser = await launch(); });
after(async () => { await browser?.close(); srv?.stop(); });

const SUBS = ['chat', 'flow', 'tasks', 'agents', 'tabs', 'lineage', 'attention', 'progress', 'links', 'proposals', 'people'];
async function hostile(browser, state) {
  const used = allUsed;
  const { ctx, page } = await openReal(browser, srv.info, 'alice', { viewport: { width: 1280, height: 900 }, reducedMotion: 'reduce' }, async (c) => {
    await c.route('**/api/v1/collab/**', async (route) => {
      try {
        if (route.request().method() !== 'GET') return await route.continue();
        const res = await route.fetch();
        if (!/json/.test(res.headers()['content-type'] || '') || /\/collab\/config(\?|$)/.test(route.request().url())) return await route.fulfill({ response: res });   // the sign-in config is how the page finds its own server
        let body; try { body = await res.json(); } catch { return await route.fulfill({ response: res }); }
        // a room with no linked mission has no milestone to show: give the progress view one, so its label renders
        if (/\/progress(\?|$)/.test(route.request().url()) && body && Array.isArray(body.milestones) && !body.milestones.length) {
          body.milestones.push({ ...(body.room || {}), milestone: 'mission:m1', label: 'Milestone one', tasks: 1, complete: 0 });
        }
        return await route.fulfill({ response: res, json: taint(body, '', used) });
      } catch { /* the page went away mid-request */ }
    });
  });
  return { ctx, page, used };
}

/** What a payload that got through would leave behind. */
const damage = (page) => page.evaluate(() => ({
  executed: window.__xss,
  elements: [...document.querySelectorAll('img[src="x"], [data-cx-attr], [id^="cx-canary"], [onerror], [onload], [onmouseover], [onclick], [onfocus], a[href^="javascript:" i], iframe, object, embed')].map((e) => `${e.tagName.toLowerCase()}${e.id ? '#' + e.id : ''}`),
  inlineScripts: document.querySelectorAll('script:not([src])').length,
}));
const shownKeys = (page) => page.evaluate(() => { const h = document.documentElement.innerHTML; return [...new Set([...h.matchAll(/cx-canary-([a-z0-9]+)/gi)].map((m) => m[1]))]; });

const seen = new Map();      // field -> where its canary was visible
const note = (where, keys) => { for (const k of keys) { if (!seen.has(k)) seen.set(k, new Set()); seen.get(k).add(where); } };

test('every surface shows hostile text as text and creates nothing from it', async () => {
  const { ctx, page, used } = await hostile(browser);
  const bad = [];
  for (const sub of SUBS) {
    await openSub(page, sub);
    await settled(page, { quietMs: 300 });
    const d = await damage(page);
    note(sub, await shownKeys(page));
    if (d.executed !== undefined || d.elements.length || d.inlineScripts) bad.push(`[${sub}] executed=${d.executed} elements=${d.elements.join(',')} inlineScripts=${d.inlineScripts}`);
  }
  assert.deepEqual(bad, []);
  assert.ok(used.size >= 8, `the proxy rewrote fields of ${used.size} kinds: ${[...used].join(', ')}`);
  await ctx.unrouteAll({ behavior: 'ignoreErrors' });
  await ctx.close();
});

for (const st of STATES) {
  test(`hostile text is inert in the state: ${st.id}`, async () => {
    const { ctx, page } = await hostile(browser);
    await st.open(page);
    await settled(page, { quietMs: 300 });
    for (const sel of st.must) assert.ok(await page.locator(sel).count() > 0, `did not reach the state: "${sel}" is missing`);
    const d = await damage(page);
    note(st.id, await shownKeys(page));
    assert.equal(d.executed, undefined, 'no payload executed');
    assert.deepEqual(d.elements, [], 'no element or handler was created from a payload');
    assert.equal(d.inlineScripts, 0, 'no inline script appeared');
    await ctx.unrouteAll({ behavior: 'ignoreErrors' });
    await ctx.close();
  });
}

// Where a particular canary must be visible: one entry per renderer that earlier slipped through because nothing put hostile text there.
const PLACEMENTS = [
  ['tabs', 'a remote agent tab: the lineage it reported, named by the agent label', '[aria-label^="Lineage reported by"]', 'cx-canary-label',
    async (page) => { await openSub(page, 'tabs'); await page.locator('#hcn-tabs [role="tab"]', { hasText: /Dana/ }).first().click(); await page.locator('[aria-label^="Lineage reported by"]').first().waitFor(); }],
  // round 3: four renderers whose escaping could be removed with every gate green, because no fixture put text where they print it
  ['tabs', 'a virtual tab: who reported the agent (reported_by)', '#hcn-tabpanel', 'cx-canary-reportedby',
    async (page) => { await openSub(page, 'tabs'); await page.locator('#hcn-tabs [role="tab"]', { hasText: /Virtual agent/ }).first().click(); await page.locator('#hcn-tabpanel', { hasText: 'Virtual tab' }).waitFor(); }],
  ['lineage', 'the reason a node is in its state (state_reason)', '.lin-inspector', 'cx-canary-statereason',
    async (page) => { await openSub(page, 'lineage'); await page.locator('svg.lin-svg g[data-node]').first().waitFor(); await page.locator('g[data-node="attempt:a2"]').focus(); await page.keyboard.press('Enter'); await page.locator('.lin-inspector').waitFor(); }],
  // T16 gap D: the asker/target reference nodes of a consultation carry attrs.consultation (the id of the thread node they belong to).
  // The inspector lists every attribute of a node, so it IS shown, escaped, once a reference node is opened.
  ['lineage', 'a consultation reference node: the attribute that names its thread (attrs.consultation)', '.lin-inspector', 'cx-canary-consultation',
    async (page) => {
      await openSub(page, 'lineage');
      const ref = page.locator('svg.lin-svg g[data-node^="thread:hcn-consult-asker."]');
      await ref.first().waitFor(); await ref.first().focus(); await page.keyboard.press('Enter'); await page.locator('.lin-inspector').waitFor();
    }],
  ['agents', 'a pending agent request: its owner (owner_email)', '#hcn-requests', 'cx-canary-owneremail',
    async (page) => { await openSub(page, 'agents'); await page.locator('#hcn-requests li').first().waitFor(); }],
  ['flow', 'the detail of an agent node in the flow: its owner (owner_email)', '#hcn-flow-detail', 'cx-canary-owneremail',
    async (page) => {
      await openSub(page, 'flow');
      const nodes = page.locator('svg.cx-flow [data-node]'); await nodes.first().waitFor();
      for (let i = 0, n = await nodes.count(); i < n; i += 1) {
        await nodes.nth(i).focus(); await page.keyboard.press('Enter');
        if ((await page.locator('#hcn-flow-detail').innerText()).includes('cx-canary-owneremail')) return;
      }
    }],
  ['progress', 'the analytics table: the basis of a figure (basis)', '#hcn-analytics', 'cx-canary-basis',
    async (page) => { await openSub(page, 'progress'); await page.locator('#hcn-analytics table td').first().waitFor(); }],
  ['progress', 'a milestone label', '[aria-label="Milestones"]', 'cx-canary-label', async (page) => { await openSub(page, 'progress'); await page.locator('[aria-label="Milestones"]').first().waitFor(); }],
  ['proposals', 'the target of a bounded task', '[aria-label="Bounded tasks"]', 'cx-canary-targetname', async (page) => { await openSub(page, 'proposals'); await page.locator('[aria-label="Bounded tasks"]').first().waitFor(); }],
];
for (const [, what, sel, canary, open] of PLACEMENTS) {
  test(`a canary is visible, as text, in ${what}`, async () => {
    const { ctx, page } = await hostile(browser);
    await open(page);
    await page.waitForFunction(([s, c]) => (document.querySelector(s)?.innerText || '').includes(c), [sel, canary], { timeout: 5000 }).catch(() => {});
    const shown = await page.locator(sel).first().innerText();
    assert.ok(shown.includes(canary), `${canary} is not on screen inside ${sel}: ${shown.slice(0, 120)}`);
    note(`placement: ${what}`, await shownKeys(page));
    const d = await damage(page);
    assert.equal(d.executed, undefined); assert.deepEqual(d.elements, []);
    await ctx.unrouteAll({ behavior: 'ignoreErrors' });
    await ctx.close();
  });
}

// Fields the server sends and the proxy taints that no surface or state of THIS fixture paints. Every one is covered somewhere else
// (ui-gates-render-xss.test.mjs): a renderer prints it (error, method, milestone, paths: the canary is asserted visible when a complete
// model is rendered), it is drawn only as a number (progress), or no page source reads it at all (UNREAD, scanned there). The list
// must match what the run finds: a field that starts to render here must be removed, a new one added on purpose.
const UNRENDERED = ['error', 'method', 'milestone', 'paths', 'progress', ...Object.keys(UNREAD).map(tag)].sort();

test('the canaries really reached the screen for a wide set of fields (otherwise "inert" proves nothing)', () => {
  const table = [...seen].map(([k, w]) => `${k}: ${[...w].map((x) => x.slice(0, 24)).join(' | ')}`).sort();
  console.log(`canary fields seen on screen (${table.length}):\n  ${table.join('\n  ')}`);
  const REQUIRED = ['instruction', 'workspaceid', 'label', 'title', 'text', 'name', 'question', 'lastactivity', 'content'];
  for (const k of REQUIRED) assert.ok(seen.has(k), `no canary for "${k}" ever reached the screen: a renderer that stops escaping it would go unnoticed`);
  const unseen = [...new Set([...allUsed].filter((k) => !URLS.has(k)).map(tag))].filter((k) => !seen.has(k)).sort();
  console.log(`fields tainted but never visible on any surface or state (${unseen.length}): ${unseen.join(', ')}`);
  assert.deepEqual(unseen, UNRENDERED, 'a field the proxy taints must reach the screen somewhere, or be listed (and justified) in UNRENDERED: a field that is never shown cannot be tested by this gate');
  assert.ok(seen.size >= 10, `only ${seen.size} fields reached the screen`);
});
