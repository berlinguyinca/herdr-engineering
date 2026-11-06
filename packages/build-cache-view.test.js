import test from 'node:test';
import assert from 'node:assert/strict';
import { bytes, cacheCounts, writeTotals, renderBuildCache, renderPreview, buildHref, pageFailure } from './build-cache-view.js';

test('cacheCounts sums only measured sccache counters and never invents a rate', () => {
  const r = cacheCounts([
    { status: 'completed', sccache_stats: { 'cache_hits.counts.Rust': 3, 'cache_misses.counts.Rust': 1, 'cache_hits.counts.C': 2 } },
    { status: 'completed', sccache_stats: { 'cache_hits.counts.Rust': 0 } },
    { status: 'running_or_unknown', sccache_stats: { 'cache_hits.counts.Rust': 99 } },
  ]);
  assert.equal(r.hits, 5);
  assert.equal(r.misses, 1);
  assert.equal(r.measured, 2);
  assert.ok(Math.abs(r.rate - 5 / 6) < 1e-9);
  const none = cacheCounts([{ status: 'completed', unavailable: ['sccache_stats'] }]);
  assert.equal(none.rate, null);
  assert.match(none.reason, /unavailable for 1 of 1/);
  assert.equal(cacheCounts([]).rate, null);
});

test('writeTotals keeps the two figures apart and a missing value is not zero', () => {
  const w = writeTotals([
    { status: 'completed', proc_io_write_bytes: 100, host_disk_write_bytes_delta: null },
    { status: 'completed', proc_io_write_bytes: 50 },
    { status: 'running_or_unknown', proc_io_write_bytes: 1000 },
  ]);
  assert.deepEqual(w.logical, { total: 150, measured: 2, of: 2 });
  assert.deepEqual(w.disk, { total: null, measured: 0, of: 2 });
});

test('bytes and buildHref', () => {
  assert.equal(bytes(null), null);
  assert.equal(bytes(512), '512 B');
  assert.equal(bytes(2048), '2.0 KiB');
  assert.equal(buildHref('a/b c'), '#/build-cache/a%2Fb%20c');
});

test('server strings are escaped in the page and the preview', () => {
  const evil = '<img src=x onerror=alert(1)>';
  const html = renderBuildCache({
    status: { data: { summary: {} } }, hosts: { data: { hosts: [{ host_id: evil }] } },
    builds: [{ build_id: evil, status: 'completed', trace: { session: evil, mission: evil }, cache: { reasons: [evil] } }], more: false,
  });
  assert.ok(!html.includes('<img'));
  const prev = renderPreview({ status: evil, reason: evil, plan: { candidates: [{ id: evil, bytes: 1, reason: evil }] } });
  assert.ok(!prev.includes('<img'));
  assert.match(prev, /CLI-only/);
});

test('pageFailure maps 401/403/501 to explicit kinds and ignores partial failures', () => {
  const ok = { error: null };
  assert.equal(pageFailure({ status: { error: 'x', status: 401 }, hosts: ok, builds: ok }).kind, 'auth');
  assert.equal(pageFailure({ status: { error: 'x', status: 403 }, hosts: ok, builds: ok }).kind, 'denied');
  assert.equal(pageFailure({ status: { error: 'x', status: 501 }, hosts: ok, builds: ok }).kind, 'disabled');
  assert.equal(pageFailure({ status: ok, hosts: { error: 'h' }, builds: ok }), null);
});

test('builds table cells carry their column name and explicit ARIA roles for the stacked phone layout', () => {
  const html = renderBuildCache({ status: { data: { summary: {} } }, hosts: { data: { hosts: [] } }, builds: [{ build_id: 'b1', status: 'completed' }], more: false });
  const row = html.slice(html.indexOf('<tr role="row" data-bc-build="b1">'), html.indexOf('</tr>', html.indexOf('data-bc-build="b1"')));
  const labels = [...row.matchAll(/data-label="([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(labels, ['Build', 'Status', 'Command', 'Host', 'Time', 'Elapsed', 'Cache', 'Logical writes (lower bound)', 'Host disk delta', 'Trace']);
  assert.match(html, /<table class="hd-table" role="table" data-bc-builds>/);
});
