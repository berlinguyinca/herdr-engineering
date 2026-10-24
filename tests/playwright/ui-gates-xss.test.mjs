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
import { launch, openReal, openSub, startRealServer } from './ui-gates-lib.mjs';
import { STATES } from './ui-gates-states.mjs';

const allUsed = new Set();      // every field the proxy tainted, over all tests
let srv; let browser;
before(async () => { srv = await startRealServer({ rich: true, mention_agent: true }); browser = await launch(); });
after(async () => { await browser?.close(); srv?.stop(); });

const SUBS = ['chat', 'flow', 'tasks', 'agents', 'tabs', 'lineage', 'attention', 'progress', 'links', 'proposals', 'people'];
// EVERY string the server sends is tainted unless it is an identifier or a value the page branches on (an id, a fence, a digest, a
// state, a kind, a timestamp, a handle used to look something up): changing those breaks the page rather than testing it. The
// earlier version listed the display fields by hand, and a field nobody listed (a bounded task's target_name) was never tested.
const KEEP = /(^|_)(id|ids|key|fence|digest|sha|token|state|status|kind|type|role|action|actions|mode|family|visibility|source|scope|version|revision|cursor|offset|code|stage|tone|level|severity|schema|handle|instance|device|credential|ref|fp|cmd|channel|transport|direction|format|mime|encoding|lang|tz|unit|tier|phase|outcome|result|verdict|decision|trust|origin|namespace|prefix|suffix|field|fields|path_kind|edge|edge_type|node_type|anchor|target|subject|expected|binding|principal|session|surface|pane|tab|workspace|from|to|by|domain|issuer|audience|public_url)$|_at$|^(next|prev|since|until|ts|time|ttl|seq|sequence|ok|id|n|kind|type|state|status|role)$/;
// Identifier-looking keys that ARE shown to people and that the server restricts, so only a proxy can test them.
const FORCE = new Set(['workspace_id', 'host', 'instance_id', 'display_name', 'email', 'reported_by', 'agent_name', 'delegate_label', 'owner_label', 'role_label', 'last_activity', 'target_name', 'attestor']);
const URLS = new Set(['url', 'href', 'link_url', 'pr_url', 'html_url', 'web_url']);
const tag = (k) => String(k).replace(/[^a-z0-9]/gi, '').slice(0, 24);
// The payload breaks out of both contexts: a double quote first (out of an attribute value, leaving an event handler and a marker
// attribute behind), then markup (out of text). Escaped, it is just characters.
const payload = (k) => `" onfocus=window.__xss=1 data-cx-attr x="'><img src=x onerror=window.__xss=1><b id=cx-canary-${tag(k)}>`;

function taint(v, key, used) {
  if (typeof v === 'string') {
    if (URLS.has(key)) { used.add(key); return 'javascript:window.__xss=9'; }
    if (v.length && key && (FORCE.has(key) || !KEEP.test(key)) && !/^[0-9a-f-]{8,}$/i.test(v) && !/^\d{4}-\d\d-\d\dT/.test(v)) { used.add(key); return `${payload(key)} ${v}`; }
    return v;
  }
  if (Array.isArray(v)) return v.map((x) => taint(x, key, used));
  if (v && typeof v === 'object') { const o = {}; for (const [k, x] of Object.entries(v)) o[k] = taint(x, k, used); return o; }
  return v;
}

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
    await page.waitForTimeout(700);
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
    await page.waitForTimeout(700);
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
  ['progress', 'a milestone label', '[aria-label="Milestones"]', 'cx-canary-label', async (page) => { await openSub(page, 'progress'); await page.locator('[aria-label="Milestones"]').first().waitFor(); }],
  ['proposals', 'the target of a bounded task', '[aria-label="Bounded tasks"]', 'cx-canary-targetname', async (page) => { await openSub(page, 'proposals'); await page.locator('[aria-label="Bounded tasks"]').first().waitFor(); }],
];
for (const [, what, sel, canary, open] of PLACEMENTS) {
  test(`a canary is visible, as text, in ${what}`, async () => {
    const { ctx, page } = await hostile(browser);
    await open(page);
    await page.waitForTimeout(500);
    const shown = await page.locator(sel).first().innerText();
    assert.ok(shown.includes(canary), `${canary} is not on screen inside ${sel}: ${shown.slice(0, 120)}`);
    const d = await damage(page);
    assert.equal(d.executed, undefined); assert.deepEqual(d.elements, []);
    await ctx.unrouteAll({ behavior: 'ignoreErrors' });
    await ctx.close();
  });
}

// Fields the server sends and the proxy taints that no surface or state paints (found by the run itself, then checked by hand): they
// are consumed as data by the page, or shown only in states this gate does not open. Keep the list honest: a new unrendered field
// must be added here on purpose, and one that starts to render must be removed.
// Honest gap: these 21 are NOT proven inert by this gate. Most are analytics and review-evidence tables (basis, value, category, order,
// method, means, selfreports, reviewquorum, progress) that the small fixture room suppresses (the page shows "too few items"), PR and
// diff statistics (head, changedfiles, diffsummary, touchedpaths, paths) shown only in states with a real PR, and identifiers the page
// reads but does not paint (milestone, owneremail, reportedby, statereason, bindings, error). See docs/operations/ui-gates.md.
const UNRENDERED = ['basis', 'bindings', 'categories', 'category', 'changedfiles', 'diffsummary', 'error', 'head', 'means', 'method', 'milestone', 'order', 'owneremail',
  'paths', 'progress', 'reportedby', 'reviewquorum', 'selfreports', 'statereason', 'touchedpaths', 'value'];

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
