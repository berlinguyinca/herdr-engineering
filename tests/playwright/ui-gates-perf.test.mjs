// T16 UI gates: large-graph performance on the REAL page. 2k / 5k / 10k nodes are written through the real ingest service
// (tests/playwright/real_server.py `graph_nodes`), then the lineage tab is opened as a room owner and the browser is
// measured: first render, every "Load more" page up to the whole graph, long tasks, key-to-frame latency while filtering,
// JS heap. Also proves that above MAX_RENDER_NODES the SVG is skipped and the table alternative is shown and usable.
//
// Budgets live in ui-gates.yaml (perf:). The measurements are written to .autospec/reports/ui-gates-perf.json (not committed).
// Run: node --test --test-concurrency=1 tests/playwright/ui-gates-perf.test.mjs
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { CFG, REPO, launch, openReal, startRealServer } from './ui-gates-lib.mjs';

const SIZES = [2000, 5000, 10000];
const report = { machine: { cpu: os.cpus()[0].model, threads: os.cpus().length, memGB: Math.round(os.totalmem() / 2 ** 30), platform: `${os.type()} ${os.release()}` }, runs: [] };
let browser;
before(async () => { browser = await launch(); report.machine.chromium = browser.version(); });
after(async () => {
  await browser?.close();
  const dir = path.join(REPO, '.autospec', 'reports'); mkdirSync(dir, { recursive: true });
  writeFileSync(path.join(dir, 'ui-gates-perf.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report));
});

/** How slow this machine is right now, as a multiple of the reference (ui-gates.yaml perf.calibration_ref_ms): a fixed CPU-bound loop
 *  timed in the page. Timing budgets scale with it, so a loaded CI box does not flake while a real regression (an order of
 *  magnitude, not 2x) still fails. The median of five runs, because one run on a busy host is noise. */
async function slowdown(page) {
  const runs = [];
  for (let i = 0; i < 5; i += 1) runs.push(await page.evaluate(() => { const t = performance.now(); let x = 0; for (let k = 0; k < 20000000; k += 1) x = (x * 31 + k) % 1000003; return performance.now() - t + (x < 0 ? 1 : 0); }));
  runs.sort((a, b) => a - b);
  return Math.max(1, runs[2] / CFG.perf.calibration_ref_ms);
}
const median = (xs) => { const s = [...xs].sort((a, b) => a - b); return s.length ? s[Math.floor(s.length / 2)] : 0; };
const pct = (xs, p) => { const s = [...xs].sort((a, b) => a - b); return s.length ? s[Math.min(s.length - 1, Math.floor(p * s.length))] : 0; };

for (const n of SIZES) {
  test(`${n} nodes: load, render, page through everything, filter; SVG skipped above the cap and the table works`, { timeout: 600000 }, async () => {
    const srv = await startRealServer({ graph_nodes: n });
    try {
      const { ctx, page, w } = await openReal(browser, srv.info, 'alice');
      const scale = await slowdown(page);
      await page.evaluate(() => {
        window.__lt = [];
        new PerformanceObserver((l) => l.getEntries().forEach((e) => window.__lt.push(e.duration))).observe({ type: 'longtask', buffered: true });
        window.__lat = [];
        window.addEventListener('keydown', (e) => { const t = e.timeStamp; requestAnimationFrame(() => requestAnimationFrame(() => window.__lat.push(performance.now() - t))); }, true);
      });
      const bytes = [];
      page.on('response', async (r) => { if (/graph\/lineage\?/.test(r.url())) { try { bytes.push((await r.body()).length); } catch { /* navigation */ } } });
      const ltBefore = (await page.evaluate(() => window.__lt)).length;

      // first render of the lineage tab
      const t0 = Date.now();
      await page.locator('[data-sub="lineage"]').click();
      await page.locator('#hcn-lineage svg.lin-svg g[data-node]').first().waitFor({ timeout: 60000 });
      const firstRender = Date.now() - t0;
      assert.ok(await page.locator('svg.lin-svg g[data-node]').count() <= 300, 'first page is drawn as an SVG');

      // page through the whole graph with the keyboard (Load more)
      const per = []; let pages = 1; const tAll = Date.now();
      for (; pages < 80; pages += 1) {
        const more = page.locator('[data-more]');
        if (!(await more.count())) break;
        const shown = await page.evaluate(() => (document.querySelector('#hcn-lineage')?.innerText.match(/Showing (\d+) of/) || [])[1]);
        const t = Date.now();
        await more.focus(); await page.keyboard.press('Enter');
        await page.waitForFunction((was) => { const m = document.querySelector('#hcn-lineage')?.innerText.match(/Showing (\d+) of/); return !m || Number(m[1]) > Number(was || 0) || !document.querySelector('[data-more]'); }, shown, { timeout: 60000 });
        per.push(Date.now() - t);
      }
      const loadAll = Date.now() - tAll;
      const text = await page.locator('#hcn-lineage').innerText();
      const loaded = Number((text.match(/(\d+) nodes/) || [])[1] || 0);

      // above the cap: no SVG, an explicit message and the table alternative
      assert.equal(await page.locator('#hcn-lineage svg.lin-svg g[data-node]').count(), 0, 'the SVG graph is skipped above the cap');
      assert.match(text, /more than the 300 drawn/);
      await page.getByRole('button', { name: 'Show as table' }).first().focus();
      await page.keyboard.press('Enter');
      const tt = Date.now();
      await page.locator('table.lin-table').first().waitFor();
      const tableMs = Date.now() - tt;
      const rows = await page.locator('table.lin-table tbody tr').count();
      assert.ok(rows > 0 && rows <= 2000, `table rows are bounded (${rows})`);
      assert.match(await page.locator('#hcn-lineage').innerText(), /Showing the first 1000 rows|nodes/);

      // filtering stays responsive: type into the search box, one key at a time
      await page.evaluate(() => { window.__lat.length = 0; });
      await page.locator('[data-filter="q"]').focus();
      await page.keyboard.type('bulk12', { delay: 40 });
      await page.waitForTimeout(400);
      const lat = await page.evaluate(() => window.__lat.slice());
      const lt = (await page.evaluate(() => window.__lt)).slice(ltBefore);
      const heapMB = await page.evaluate(() => Math.round((performance.memory?.usedJSHeapSize || 0) / 2 ** 20));
      const run = { nodes: n, hostSlowdown: Number(scale.toFixed(2)), serverIngestSeconds: srv.info.ingest_seconds, firstRenderMs: firstRender, firstPageBytes: bytes[0], pagesLoaded: pages, loadAllMs: loadAll,
        loadMoreMs: { p50: pct(per, 0.5), p95: pct(per, 0.95), max: Math.max(...per, 0) }, tableSwitchMs: tableMs, tableRows: rows, nodesLoaded: loaded,
        keyToFrameMs: { p50: Math.round(pct(lat, 0.5)), p95: Math.round(pct(lat, 0.95)), max: Math.round(Math.max(...lat, 0)) },
        longTasks: { count: lt.length, maxMs: Math.round(Math.max(...lt, 0)), totalMs: Math.round(lt.reduce((a, b) => a + b, 0)) }, jsHeapMB: heapMB };
      report.runs.push(run);
      console.log(JSON.stringify(run));

      // budgets
      // The server bounds offset paging of one room's lineage (scan limit); beyond it the UI must SAY so rather than pretend.
      const reachable = Math.min(n, 3000);
      assert.ok(loaded >= reachable * 0.99, `paging reaches ${loaded} of ${n} nodes (expected at least ${reachable})`);
      if (n > 3000) assert.match(text, /Showing \d+ nodes \(\w+\)\. The server stopped reading here/, 'a truncated graph says it is truncated');
      // budgets scale with how slow the host is right now (hostSlowdown >= 1); the report records the factor used
      const B = (ms) => Math.round(ms * scale);
      assert.ok(firstRender <= B(CFG.perf.render_ms), `first render ${firstRender}ms <= ${B(CFG.perf.render_ms)}ms (x${scale.toFixed(1)} slowdown)`);
      assert.ok(run.keyToFrameMs.p95 <= B(CFG.perf.interaction_ms), `filter key-to-frame p95 ${run.keyToFrameMs.p95}ms <= ${B(CFG.perf.interaction_ms)}ms`);
      assert.ok(run.loadMoreMs.p95 <= B(CFG.perf.load_more_p95_ms), `Load more p95 ${run.loadMoreMs.p95}ms <= ${B(CFG.perf.load_more_p95_ms)}ms`);
      assert.ok(lt.length <= CFG.perf.max_long_tasks, `long tasks ${lt.length} <= ${CFG.perf.max_long_tasks}`);
      assert.ok(run.longTasks.maxMs <= B(CFG.perf.max_long_task_ms), `longest task ${run.longTasks.maxMs}ms <= ${B(CFG.perf.max_long_task_ms)}ms`);
      await w.collectCsp();
      assert.deepEqual([w.errors, w.csp, w.failed], [[], [], []], 'no console errors, CSP violations or failed requests at scale');
      await ctx.close();
    } finally { srv.stop(); }
  });
}

// The layout of a graph this size is expensive, and above the draw cap nothing is drawn from it. Typing is debounced, but a checkbox is
// not: it must redraw at once, so it is the path that proves the layout is skipped when there is no SVG to lay out for.
test('toggling a type filter on a graph above the draw cap is cheap: the layout is not computed for a graph that is not drawn', { timeout: 600000 }, async () => {
  const srv = await startRealServer({ graph_nodes: 3500 });
  try {
    const { ctx, page } = await openReal(browser, srv.info, 'alice');
    const scale = await slowdown(page);
    await page.locator('[data-sub="lineage"]').click();
    await page.locator('#hcn-lineage svg.lin-svg g[data-node]').first().waitFor({ timeout: 60000 });
    for (let i = 0; i < 40 && await page.locator('[data-more]').count(); i += 1) {
      const before = await page.locator('#hcn-lineage').innerText();
      await page.locator('[data-more]').focus(); await page.keyboard.press('Enter');
      await page.waitForFunction((b) => document.querySelector('#hcn-lineage')?.innerText !== b, before, { timeout: 60000 });
    }
    assert.equal(await page.locator('#hcn-lineage svg.lin-svg g[data-node]').count(), 0, 'above the cap, so nothing is drawn');
    await page.evaluate(() => { window.__t = []; });
    const box = page.locator('input[data-filter="type"][value="task"]');
    await box.focus();
    const times = [];
    for (let i = 0; i < 6; i += 1) {
      const t = await page.evaluate(() => new Promise((resolve) => { const t0 = performance.now(); document.activeElement.click(); requestAnimationFrame(() => requestAnimationFrame(() => resolve(performance.now() - t0))); }));
      times.push(Math.round(t));
      await page.locator('input[data-filter="type"][value="task"]').focus();
    }
    const med = median(times);
    console.log(JSON.stringify({ toggleFilterMs: times, median: med, hostSlowdown: Number(scale.toFixed(2)) }));
    report.toggle = { nodes: 3500, ms: times, median: med, hostSlowdown: Number(scale.toFixed(2)) };
    assert.ok(med <= Math.round(CFG.perf.toggle_ms * scale), `median ${med}ms per filter toggle <= ${Math.round(CFG.perf.toggle_ms * scale)}ms (${times.join(', ')})`);
    await ctx.close();
  } finally { srv.stop(); }
});
