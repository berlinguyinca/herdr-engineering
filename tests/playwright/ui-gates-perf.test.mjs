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

const pct = (xs, p) => { const s = [...xs].sort((a, b) => a - b); return s.length ? s[Math.min(s.length - 1, Math.floor(p * s.length))] : 0; };

for (const n of SIZES) {
  test(`${n} nodes: load, render, page through everything, filter; SVG skipped above the cap and the table works`, { timeout: 600000 }, async () => {
    const srv = await startRealServer({ graph_nodes: n });
    try {
      const { ctx, page, w } = await openReal(browser, srv.info, 'alice');
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
      const run = { nodes: n, serverIngestSeconds: srv.info.ingest_seconds, firstRenderMs: firstRender, firstPageBytes: bytes[0], pagesLoaded: pages, loadAllMs: loadAll,
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
      assert.ok(firstRender <= CFG.perf.render_ms, `first render ${firstRender}ms <= ${CFG.perf.render_ms}ms`);
      assert.ok(run.keyToFrameMs.p95 <= CFG.perf.interaction_ms, `filter key-to-frame p95 ${run.keyToFrameMs.p95}ms <= ${CFG.perf.interaction_ms}ms`);
      assert.ok(lt.length <= CFG.perf.max_long_tasks, `long tasks ${lt.length} <= ${CFG.perf.max_long_tasks}`);
      assert.ok(run.longTasks.maxMs <= CFG.perf.max_long_task_ms, `longest task ${run.longTasks.maxMs}ms <= ${CFG.perf.max_long_task_ms}ms`);
      await w.collectCsp();
      assert.deepEqual([w.errors, w.csp, w.failed], [[], [], []], 'no console errors, CSP violations or failed requests at scale');
      await ctx.close();
    } finally { srv.stop(); }
  });
}
