// Client-side cost of the lineage viewport with 5,000 and 10,000 nodes LOADED IN THE BROWSER (S5 perf record).
// The real server caps how far offset paging reaches (~3,000 nodes of one room, see ui-gates-perf), so to measure the client at 5k/10k
// this test answers the page's own GET /graph/lineage requests from a synthetic graph, 200 nodes a page, exactly as the server pages.
// Everything else is the real assembled page (real_server.py, real JS). It prints one JSON line per run (nothing is asserted about time:
// the numbers are recorded in docs/specs/.../perf.md); it asserts only the bounds that must hold at any size.
// Run: node --test --test-concurrency=1 tests/perf/lineage-scale.mjs  (hand-run; not part of any npm script)
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import os from 'node:os';
import { launch, openReal, openSub, startRealServer } from '../playwright/ui-gates-lib.mjs';

let srv; let browser;
before(async () => { srv = await startRealServer({ fixed_clock: true, empty: true }); browser = await launch(); });
after(async () => { await browser?.close(); srv?.stop(); });

// shapes: `tree` is shallow and wide (depth ~14), `chains` is 50-long chains under one root (depth 51, ~n/50 rows)
function synth(n, shape) {
  const nodes = []; const edges = [];
  for (let i = 0; i < n; i += 1) {
    nodes.push({ id: `task:n${i}`, node_type: 'task', label: `Task ${i}`, state: ['running', 'done', 'blocked'][i % 3], attrs: {}, visibility_scope: 'room', fields: {}, first_event_id: `e${i}`, last_event_id: `e${i}` });
    if (!i) continue;
    const parent = shape === 'tree' ? Math.floor((i - 1) / 2) : (i % 50 === 1 ? 0 : i - 1);
    edges.push({ edge_type: 'dependency', from_id: `task:n${parent}`, to_id: `task:n${i}`, attrs: {}, stale: false, removed: false });
  }
  return { nodes, edges };
}
const PAGE = 200;
const q = (xs, p) => { const s = [...xs].sort((a, b) => a - b); return s.length ? Math.round(s[Math.min(s.length - 1, Math.floor(p * s.length))]) : 0; };

for (const shape of ['tree', 'chains']) for (const n of [5000, 10000]) {
  test(`${n} nodes loaded in the client (${shape}): bounded DOM, measured pan / zoom / minimap / select cost`, { timeout: 900000 }, async () => {
    const g = synth(n, shape);
    const { ctx, page } = await openReal(browser, srv.info, 'alice', { reducedMotion: 'reduce' });
    await page.route(/\/graph\/lineage\?/, async (route) => {
      const off = Number(new URL(route.request().url()).searchParams.get('offset') || 0);
      const nodes = g.nodes.slice(off, off + PAGE); 
      const mine = new Set(nodes.map((x) => x.id));
      const here = g.edges.filter((e) => mine.has(e.to_id));                       // each edge arrives with its target's page
      await route.fulfill({ json: { nodes, edges: here, as_of: { step: 1, visible_events: 1, head: true }, total_nodes: n, next_offset: off + PAGE < n ? off + PAGE : null, truncated_reason: off + PAGE < n ? 'limit' : null } });
    });
    await openSub(page, 'lineage');
    await page.locator('svg.lin-svg g[data-node]').first().waitFor({ timeout: 60000 });
    await page.evaluate(() => {
      window.__lt = []; new PerformanceObserver((l) => l.getEntries().forEach((e) => window.__lt.push(e.duration))).observe({ type: 'longtask', buffered: true });
      window.__frame = (fn) => new Promise((resolve) => { const t = performance.now(); fn(); requestAnimationFrame(() => requestAnimationFrame(() => resolve(performance.now() - t))); });
    });
    const t0 = Date.now();
    let pages = 1;
    for (; pages < 200; pages += 1) {
      const more = await page.evaluate(() => { const b = document.querySelector('[data-more]'); const t = document.querySelector('#hcn-lineage')?.innerText; if (b) b.click(); return b ? t : null; });
      if (more === null) break;
      await page.waitForFunction((b) => document.querySelector('#hcn-lineage')?.innerText !== b, more, { timeout: 120000 });
    }
    const loadAllMs = Date.now() - t0;
    const shown = Number((await page.locator('#hcn-lineage').innerText()).match(/(\d+) nodes, \d+ links shown/)?.[1] || 0);
    assert.equal(shown, n, 'every node is loaded in the client');
    const bound = async (label) => {
      const drawn = await page.locator('[data-viewport] svg.lin-svg g[data-node]').count();
      const edges = await page.locator('[data-viewport] svg.lin-svg g.lin-edge').count();
      const els = await page.locator('[data-viewport] *').count();
      assert.ok(drawn >= 1 && drawn <= 242, `${label}: ${drawn} nodes drawn`);
      assert.ok(edges <= 480, `${label}: ${edges} links drawn`);
      return { drawn, edges, els };
    };
    const dom = await bound('fit');
    const steps = { zoomIn: [], zoomOut: [], pan: [], minimap: [], select: [] };
    for (let i = 0; i < 12; i += 1) { steps.zoomIn.push(await page.evaluate(() => window.__frame(() => document.querySelector('[data-vp="in"]').click()))); }
    await bound('zoomed in');
    for (let i = 0; i < 24; i += 1) { steps.zoomOut.push(await page.evaluate(() => window.__frame(() => document.querySelector('[data-vp="out"]').click()))); }
    const far = await bound('zoomed far out');
    for (let i = 0; i < 12; i += 1) { steps.zoomIn.push(await page.evaluate(() => window.__frame(() => document.querySelector('[data-vp="in"]').click()))); }
    // every step below is dispatched INSIDE the page and timed to the second frame after it, so the figure includes the handler, the cull and the redraw
    await page.locator('[data-viewport]').focus();
    for (let i = 0; i < 24; i += 1) {
      steps.pan.push(await page.evaluate((k) => window.__frame(() => document.querySelector('[data-viewport]').dispatchEvent(new KeyboardEvent('keydown', { key: k, shiftKey: true, bubbles: true }))), i % 2 ? 'ArrowDown' : 'ArrowRight'));
    }
    const hasMap = await page.locator('svg.lin-mm').count();
    if (hasMap) for (let i = 0; i < 12; i += 1) {
      steps.minimap.push(await page.evaluate((f) => window.__frame(() => { const m = document.querySelector('svg.lin-mm'); const r = m.getBoundingClientRect(); m.dispatchEvent(new MouseEvent('click', { bubbles: true, clientX: r.left + r.width * f, clientY: r.top + r.height * (1 - f) })); }), (i + 0.5) / 12));
    }
    const sel = await page.locator('g[data-node]').first().getAttribute('data-node');
    for (let i = 0; i < 6; i += 1) { steps.select.push(await page.evaluate((id) => window.__frame(() => document.querySelector(`g[data-node="${id}"]`)?.dispatchEvent(new MouseEvent('click', { bubbles: true }))), sel)); }
    const end = await bound('after interaction');
    const lt = await page.evaluate(() => window.__lt.slice());
    const heapMB = await page.evaluate(() => Math.round((performance.memory?.usedJSHeapSize || 0) / 2 ** 20));
    const row = { shape, nodes: n, edges: g.edges.length, pagesLoaded: pages, loadAllMs, domAtFit: dom, domFarOut: far, domEnd: end, minimapPresent: hasMap === 1,
      framesMs: Object.fromEntries(Object.entries(steps).map(([k, v]) => [k, { n: v.length, p50: q(v, 0.5), p95: q(v, 0.95), max: q(v, 1) }])),
      longTasks: { count: lt.length, maxMs: Math.round(Math.max(0, ...lt)) }, jsHeapMB: heapMB,
      host: `${os.cpus()[0].model} x${os.cpus().length}, load ${os.loadavg()[0].toFixed(1)}` };
    console.log(`SCALE ${JSON.stringify(row)}`);
    await ctx.close();
  });
}
