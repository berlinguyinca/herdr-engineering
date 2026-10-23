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

let srv; let browser;
before(async () => { srv = await startRealServer({ rich: true }); browser = await launch(); });
after(async () => { await browser?.close(); srv?.stop(); });

const SUBS = ['chat', 'flow', 'tasks', 'agents', 'tabs', 'lineage', 'attention', 'progress', 'links', 'proposals', 'people'];
// Fields that are shown to people. Identifiers the page uses to look things up (ids, fences, digests, states) are left alone:
// changing them would break the page rather than test it.
const DISPLAY = new Set(['name', 'label', 'title', 'text', 'instruction', 'content', 'reason', 'activity', 'message', 'summary', 'description', 'detail', 'note',
  'workspace_id', 'host', 'display_name', 'instance_id', 'email', 'question', 'answer', 'why', 'branch', 'delegate_label', 'reported_by', 'attestor', 'agent_name',
  'path', 'paths', 'touched_paths', 'questions', 'task', 'goal', 'what', 'owner_label', 'role_label', 'status_text', 'last_activity', 'headline', 'body', 'error', 'notice']);
const URLS = new Set(['url', 'href', 'link_url', 'pr_url', 'html_url', 'web_url']);
const tag = (k) => String(k).replace(/[^a-z0-9]/gi, '').slice(0, 24);
const payload = (k) => `<img src=x onerror=window.__xss=1><b id=cx-canary-${tag(k)}>"'>`;

function taint(v, key, used) {
  if (typeof v === 'string') {
    if (URLS.has(key)) { used.add(key); return 'javascript:window.__xss=9'; }
    if (DISPLAY.has(key) && v.length) { used.add(key); return `${payload(key)} ${v}`; }
    return v;
  }
  if (Array.isArray(v)) return v.map((x) => taint(x, key, used));
  if (v && typeof v === 'object') { const o = {}; for (const [k, x] of Object.entries(v)) o[k] = taint(x, k, used); return o; }
  return v;
}

async function hostile(browser, state) {
  const used = new Set();
  const { ctx, page } = await openReal(browser, srv.info, 'alice', { viewport: { width: 1280, height: 900 }, reducedMotion: 'reduce' }, async (c) => {
    await c.route('**/api/v1/collab/**', async (route) => {
      try {
        if (route.request().method() !== 'GET') return await route.continue();
        const res = await route.fetch();
        if (!/json/.test(res.headers()['content-type'] || '')) return await route.fulfill({ response: res });
        let body; try { body = await res.json(); } catch { return await route.fulfill({ response: res }); }
        return await route.fulfill({ response: res, json: taint(body, '', used) });
      } catch { /* the page went away mid-request */ }
    });
  });
  return { ctx, page, used };
}

/** What a payload that got through would leave behind. */
const damage = (page) => page.evaluate(() => ({
  executed: window.__xss,
  elements: [...document.querySelectorAll('img[src="x"], [id^="cx-canary"], [onerror], [onload], [onmouseover], [onclick], [onfocus], a[href^="javascript:" i], iframe, object, embed')].map((e) => `${e.tagName.toLowerCase()}${e.id ? '#' + e.id : ''}`),
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

test('the canaries really reached the screen for a wide set of fields (otherwise "inert" proves nothing)', () => {
  const table = [...seen].map(([k, w]) => `${k}: ${[...w].map((x) => x.slice(0, 24)).join(' | ')}`).sort();
  console.log(`canary fields seen on screen (${table.length}):\n  ${table.join('\n  ')}`);
  const REQUIRED = ['instruction', 'workspaceid', 'label', 'title', 'text', 'name', 'question', 'lastactivity', 'content'];
  for (const k of REQUIRED) assert.ok(seen.has(k), `no canary for "${k}" ever reached the screen: a renderer that stops escaping it would go unnoticed`);
  assert.ok(seen.size >= 10, `only ${seen.size} fields reached the screen`);
});
