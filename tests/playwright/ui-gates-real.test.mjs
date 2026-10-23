// T16 UI gates against the REAL assembled collaboration page: tests/playwright/real_server.py serves collab.html and its
// modules from the integrated control plane's public listener (real CSP), backed by a real CollabService, lineage graph,
// ingest path, workbench, contextual and insights services, with a seeded room. Earlier task PRs tested renderers
// against mocked APIs; only this file proves the shipped page boots and works end to end.
//
// Run: node --test --test-concurrency=1 tests/playwright/ui-gates-real.test.mjs
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import path from 'node:path';
import { CFG, REPO, launch, openReal, openSub, startRealServer } from './ui-gates-lib.mjs';
import { staticAudit, focusIndicatorAudit, tabTrapAudit, overflowAudit, motionAudit, summarize } from './a11y-checks.mjs';

const SUBS = ['chat', 'flow', 'tasks', 'agents', 'tabs', 'lineage', 'attention', 'progress', 'links', 'proposals', 'people'];
// Text that proves a surface actually rendered something specific (a blank panel passes every a11y check).
const PROOF = {
  chat: /Review the parser and propose a fix/i, flow: /Flow of work/i, tasks: /accepted/i, agents: /Agents in this room/i,
  tabs: /Dana's Claude/i, lineage: /Live: showing all \d+ events/i, attention: /2 items need attention/i,
  progress: /Estimate/i, links: /ws-1 on bender/i, proposals: /Parser notes/i, people: /bob/i,
};
let srv; let browser; let info;
before(async () => { srv = await startRealServer({}); info = srv.info; browser = await launch(); });
after(async () => { await browser?.close(); srv?.stop(); });

async function session(user, opts = {}) {
  const s = await openReal(browser, info, user, opts);
  return s;
}
const render = async (page, sub, user = 'alice') => {
  await openSub(page, sub);
  // a viewer sees a different set of items (e.g. no steering request waits for them), so only the owner's content is pinned
  const re = user === 'alice' || ['chat', 'lineage', 'tabs', 'links'].includes(sub) ? PROOF[sub].source : '\\S{3}';
  await page.waitForFunction(([id, re]) => new RegExp(re, 'i').test(document.querySelector(`#hcn-p-${id}`)?.innerText || ''), [sub, re], { timeout: 10000 });
  if (sub === 'lineage') await page.locator('svg.lin-svg g[data-node]').first().waitFor();
  await page.waitForTimeout(150);
};
const noProblems = async (w, label) => {
  await w.collectCsp();
  assert.deepEqual(w.errors, [], `${label}: console errors/warnings`);
  assert.deepEqual(w.csp, [], `${label}: CSP violations`);
  assert.deepEqual(w.failed, [], `${label}: failed requests`);
  assert.deepEqual(w.bad, [], `${label}: HTTP >= 400 responses`);
};

// ---------------------------------------------------------------------------------------------------------------------
test('the public listener serves every module the page imports (regression: collab-context.js / collab-workbench.js were 404)', async () => {
  const pk = path.join(REPO, 'packages');
  const todo = ['collab.html']; const seen = new Set();
  while (todo.length) {
    const f = todo.pop();
    if (seen.has(f)) continue;
    seen.add(f);
    assert.ok(existsSync(path.join(pk, f)), `${f} exists`);
    const src = readFileSync(path.join(pk, f), 'utf8');
    const refs = f.endsWith('.html') ? [...src.matchAll(/(?:src|href)="([^"]+)"/g)] : [...src.matchAll(/(?:from|import\()\s*['"]([^'"]+)['"]/g)];
    for (const [, r] of refs) if (!/^(https?:|data:)/.test(r) && /\.(js|css|html)$/.test(r)) todo.push(path.normalize(path.join(path.dirname(f), r)));
  }
  for (const f of seen) {
    const res = await fetch(`${info.base}/${f === 'collab.html' ? '' : f}`);
    assert.equal(res.status, 200, `/${f} must be served on the public listener`);
    assert.match(res.headers.get('content-security-policy') || '', /script-src 'self'/);
    await res.arrayBuffer();
  }
  assert.ok(seen.size >= 15, `walked ${seen.size} files`);
});

test('every surface renders real content and a full tour is free of console errors, CSP violations and failed requests', async () => {
  const { ctx, page, w } = await session('alice');
  for (const sub of SUBS) {
    await render(page, sub);
    assert.match(await page.locator(`#hcn-p-${sub}`).innerText(), PROOF[sub], `${sub} shows its seeded content`);
  }
  assert.ok(await page.locator('svg.lin-svg g[data-node]').count() >= 8, 'the graph shows the seeded nodes');
  await noProblems(w, 'tour as alice');
  await ctx.close();
});

for (const scheme of ['light', 'dark']) {
  for (const user of ['alice', 'carol']) {
    test(`accessibility audit of every surface: ${user} (${user === 'carol' ? 'viewer' : 'owner'}), ${scheme}`, async () => {
      const { ctx, page, w } = await session(user, { colorScheme: scheme, reducedMotion: 'reduce' });
      const subs = SUBS.filter((s) => user === 'alice' || !['proposals', 'people'].includes(s));
      const bad = [];
      for (const sub of subs) {
        await render(page, sub, user);
        const f = [...await staticAudit(page, `#hcn-p-${sub}`), ...await focusIndicatorAudit(page, `#hcn-p-${sub}`)];
        if (f.length) bad.push(`[${sub}] ${summarize(f)}`);
      }
      // the chrome around the panels (room tabs, sub tabs, header) once
      const chrome = [...await staticAudit(page, '.hd-shell__sidebar'), ...await staticAudit(page, '.cx-rtabs'), ...await staticAudit(page, '.cx-stabs')];
      if (chrome.length) bad.push(`[chrome] ${summarize(chrome)}`);
      assert.deepEqual(bad, [], `\n${bad.join('\n')}`);
      await noProblems(w, `${user} ${scheme}`);
      await ctx.close();
    });
  }
}

test('the same audit at phone (375) and tablet (768) width: target size, names, contrast, focus', async () => {
  const bad = [];
  for (const vp of [CFG.viewports.mobile, CFG.viewports.tablet]) {
    const { ctx, page } = await session('alice', { viewport: vp, hasTouch: true });
    for (const sub of SUBS) {
      await render(page, sub);
      const f = [...await staticAudit(page, `#hcn-p-${sub}`), ...await focusIndicatorAudit(page, `#hcn-p-${sub}`)];
      if (f.length) bad.push(`[${vp.width}px ${sub}] ${summarize(f)}`);
    }
    const chrome = [...await staticAudit(page, '.hd-shell__sidebar'), ...await staticAudit(page, '.cx-rtabs'), ...await staticAudit(page, '.cx-stabs')];
    if (chrome.length) bad.push(`[${vp.width}px chrome] ${summarize(chrome)}`);
    await ctx.close();
  }
  assert.deepEqual(bad, [], `\n${bad.join('\n')}`);
});

test('reflow at 320 CSS px (400% zoom of 1280) and at 200% zoom: no two-dimensional scrolling on any surface', async () => {
  const { ctx, page } = await session('alice');
  const bad = [];
  for (const [name, vp] of [['320px', CFG.viewports.reflow], ['200% zoom (640x400)', { width: 640, height: 400 }]]) {
    await page.setViewportSize(vp);
    for (const sub of SUBS) {
      await render(page, sub);
      const o = await overflowAudit(page);
      if (o.scrollWidth > o.clientWidth + 1) bad.push(`${name} [${sub}] scrollWidth ${o.scrollWidth} > ${o.clientWidth}: ${o.offenders.join(', ')}`);
    }
  }
  assert.deepEqual(bad, []);
  await ctx.close();
});

test('reduced motion is honoured on every surface, and motion happens by default when it is not requested', async () => {
  const reduced = await session('alice', { reducedMotion: 'reduce' });
  const bad = [];
  for (const sub of SUBS) {
    await render(reduced.page, sub);
    await reduced.page.locator('.hd-btn').first().hover().catch(() => {});
    const f = await motionAudit(reduced.page);
    if (f.length) bad.push(`[${sub}] ${summarize(f)}`);
  }
  assert.deepEqual(bad, []);
  assert.equal(await reduced.page.evaluate(() => { const b = document.querySelector('.hd-btn'); getComputedStyle(b).backgroundColor; b.style.background = 'rgb(1, 2, 3)'; getComputedStyle(b).backgroundColor; const n = document.getAnimations().filter((a) => a instanceof CSSTransition).length; b.style.background = ''; return n; }), 0, 'no transition runs under prefers-reduced-motion');
  await reduced.ctx.close();
  // motion.happens-by-default: with no preference a state change really runs a CSS transition, and with `reduce` it does not
  // (an inert UI would pass every other check). A style change on a button is the same thing a hover does.
  const transitions = (page) => page.evaluate(() => {
    const b = document.querySelector('.hd-btn');
    getComputedStyle(b).backgroundColor; b.style.background = 'rgb(1, 2, 3)'; getComputedStyle(b).backgroundColor;
    const n = document.getAnimations().filter((a) => a instanceof CSSTransition).length; b.style.background = ''; return n;
  });
  const normal = await session('alice', { reducedMotion: 'no-preference' });
  await render(normal.page, 'chat');
  assert.ok(await transitions(normal.page) >= 1, 'a transition runs when motion is allowed');
  await normal.ctx.close();
});

test('no keyboard trap on any surface: Tab keeps moving and wraps', async () => {
  const { ctx, page } = await session('alice');
  const bad = [];
  for (const sub of SUBS) {
    await render(page, sub);
    const r = await tabTrapAudit(page, 400);
    if (r.trapped) bad.push(`[${sub}] trapped at ${r.at}`);
    if (r.seen.length < 5) bad.push(`[${sub}] only ${r.seen.length} stops reached`);
  }
  assert.deepEqual(bad, []);
  await ctx.close();
});

test('every tablist supports the arrow keys (roving tabindex tabs are otherwise unreachable)', async () => {
  const { ctx, page } = await session('alice');
  const bad = []; let examined = 0;
  for (const sub of SUBS) {
    await render(page, sub);
    const lists = await page.locator(`#hcn-p-${sub} [role=tablist], .cx-stabs[role=tablist], .cx-rtabs[role=tablist]`).evaluateAll((els) => els.map((e, i) => { e.setAttribute('data-gate-tl', String(i)); return { i, tabs: e.querySelectorAll('[role=tab]').length, label: e.getAttribute('aria-label') }; }));
    for (const l of lists) {
      if (l.tabs < 2) continue;
      const sel = `[data-gate-tl="${l.i}"]`;
      const hidden = await page.locator(`${sel} [role=tab][tabindex="-1"]`).count();
      if (!hidden) continue;                      // all tabs are in the Tab order: reachable without arrows
      examined += 1;
      await page.locator(`${sel} [role=tab][aria-selected=true]`).first().focus();
      const before = await page.evaluate((s) => document.querySelector(`${s} [role=tab][aria-selected=true]`)?.textContent.trim(), sel);
      await page.keyboard.press('ArrowRight');
      const after = await page.evaluate((s) => ({ active: document.activeElement?.textContent.trim(), sel: document.querySelector(`${s} [role=tab][aria-selected=true]`)?.textContent.trim() }), sel);
      if (after.active === before && after.sel === before) bad.push(`[${sub}] tablist "${l.label}": ArrowRight does nothing, ${hidden} tab(s) with tabindex=-1 are unreachable by keyboard`);
    }
  }
  assert.deepEqual(bad, []);
  assert.ok(examined >= 2, `the roving-tabindex tablists (lineage views, remote tabs) were found and exercised (${examined})`);
  await ctx.close();
});

test('hostile text (HTML, event handlers, script) renders inert in the real page on every surface that shows it', async () => {
  const { ctx, page, w } = await session('alice');
  for (const sub of ['lineage', 'proposals', 'chat', 'tabs']) {
    await render(page, sub);
    if (sub === 'lineage') {
      await page.locator('g[data-node="attempt:a1"]').focus();
      await page.keyboard.press('Enter');
      await page.locator('.wb-comment').first().waitFor();
      const t = await page.locator('.wb').innerText();
      assert.ok(t.includes('<img src=x onerror='), 'the hostile comment is visible as text');
    }
    if (sub === 'proposals') {
      await page.locator('[data-proposal]').first().focus();
      await page.keyboard.press('Enter');
      await page.waitForTimeout(500);
    }
  }
  assert.equal(await page.evaluate(() => window.__xss), undefined, 'no payload executed');
  assert.equal(await page.locator('img[src="x"], .wb img, #hcn-p-proposals img, script:not([src])').count(), 0, 'no element was created from a payload');
  assert.equal(await page.locator('[onerror], [onmouseover]').count(), 0, 'no handler attribute was created from a payload');
  await noProblems(w, 'hostile text');
  await ctx.close();
});

// ---------------------------------------------------------------------------------------------------------------------
// autospec-design rules that need a dedicated check (see .autospec/design-gates.yml)
test('motion.no-unstoppable-animation: anything that moves for more than 5 seconds can be paused from the keyboard', async () => {
  const { ctx, page } = await session('alice', { reducedMotion: 'no-preference' });
  await render(page, 'flow');
  const infinite = () => page.evaluate(() => document.getAnimations().filter((a) => a.playState === 'running' && a.effect && a.effect.getComputedTiming().iterations === Infinity).length);
  assert.ok(await infinite() > 0, 'the seeded flow has a live task line, so an endless animation is running (the check would otherwise pass vacuously)');
  await page.locator('#hcn-flow-still').focus();
  await page.keyboard.press('Enter');
  assert.equal(await page.locator('#hcn-flow-still').getAttribute('aria-pressed'), 'true');
  assert.equal(await infinite(), 0, 'pausing stops every endless animation');
  await page.keyboard.press('Enter');
  assert.ok(await infinite() > 0, 'and resuming starts them again');
  await ctx.close();
});

test('motion.no-flash: no animation repeats faster than 3 times per second', async () => {
  const { ctx, page } = await session('alice', { reducedMotion: 'no-preference' });
  const bad = [];
  for (const sub of SUBS) {
    await render(page, sub);
    bad.push(...await page.evaluate((s) => document.getAnimations().filter((a) => a.effect && (a.effect.getComputedTiming().iterations > 1)).filter((a) => a.effect.getComputedTiming().duration < 334).map((a) => `[${s}] ${a.animationName || a.transitionProperty}: ${a.effect.getComputedTiming().duration}ms`), sub));
  }
  assert.deepEqual(bad, []);
  await ctx.close();
});

test('device.orientation and device.zoom-not-blocked: rotating a phone loses nothing and the viewport allows zoom', async () => {
  const { ctx, page } = await session('alice');
  const vp = await page.evaluate(() => document.querySelector('meta[name=viewport]').content);
  assert.doesNotMatch(vp, /user-scalable\s*=\s*(no|0)|maximum-scale\s*=\s*1\b/i);
  for (const size of [{ width: 375, height: 667 }, { width: 667, height: 375 }]) {
    await page.setViewportSize(size);
    for (const sub of ['chat', 'lineage', 'attention', 'tabs']) {
      await render(page, sub);
      const o = await overflowAudit(page);
      assert.ok(o.scrollWidth <= o.clientWidth + 1, `[${sub}] ${size.width}x${size.height}: ${o.offenders.join(', ')}`);
      assert.ok(await page.locator(`[data-sub="${sub}"]`).isVisible(), `[${sub}] the sub tab is still reachable after rotation`);
    }
  }
  await ctx.close();
});

test('a task line in the flow diagram opens its detail from the keyboard (it was mouse-only)', async () => {
  const { ctx, page } = await session('alice');
  await render(page, 'flow');
  const edges = await page.locator('path[data-edge]').count();
  assert.ok(edges > 0, 'the flow has a task line');
  await page.locator('path[data-edge]').first().focus();
  assert.equal(await page.locator('path[data-edge]').first().getAttribute('role'), 'button');
  const before = await page.locator('#hcn-flow-detail').innerText();
  await page.keyboard.press('Enter');
  await page.waitForFunction((b) => document.querySelector('#hcn-flow-detail').innerText !== b, before);
  await ctx.close();
});

test('review evidence bound to an older commit than the PR head is shown as stale, distinct from current evidence', async () => {
  const { ctx, page } = await session('alice');
  await render(page, 'lineage');
  await page.locator('g[data-node="pr:github.com/o/r#1"]').focus();
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => /stale/i.test(document.querySelector('#hcn-lineage aside')?.innerText || ''), null, { timeout: 8000 });
  const t = await page.locator('#hcn-lineage aside').innerText();
  assert.match(t, /stale/i);
  await ctx.close();
});
