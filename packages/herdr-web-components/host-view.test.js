import test from 'node:test';
import assert from 'node:assert/strict';
import { renderHostDetail, sparkline, formatUptime, formatBytes } from '../host-view.js';

const NOW = Date.parse('2026-10-23T12:00:00Z');
const iso = (msAgo) => new Date(NOW - msAgo).toISOString();

function reportingHost(over = {}) {
  return {
    host_id: 'beast', host_name: 'beast', tailnet_ip: '100.64.0.7', status: 'healthy',
    last_seen: iso(30_000),
    latest: {
      at: iso(30_000), cpu: 42, load1: 1.5, load5: 1.2, load15: 0.9, mem: 61,
      mem_used_b: 20 * 2 ** 30, mem_total_b: 64 * 2 ** 30, swap: 3, disk: 71,
      mounts: [{ path: '/', pct: 71, used_b: 700e9, total_b: 1e12 }, { path: '/data', pct: 40, used_b: 2e12, total_b: 5e12 }],
      uptime_s: 273600, net_rx_bps: 1200, net_tx_bps: 800, agents_running: 3,
      gpus: [{ name: 'RTX 4090', util_pct: 88, vram_used_b: 20 * 2 ** 30, vram_total_b: 24 * 2 ** 30, temp_c: 71, cores: 16384 }],
      top: {
        cpu: [{ pid: 11, command: 'llama-server', user: 'gw', cpu_pct: 380, mem_pct: 12, owner: 'service:llama' }],
        mem: [{ pid: 12, command: 'node', user: 'wohlgemuth', cpu_pct: 4, mem_pct: 22, owner: null }],
      },
    },
    updates: { manager: 'apt', outdated: 12, security: 2, packages: [{ name: 'openssl', from: '3.0.1', to: '3.0.2' }], reboot_required: true, failed_units: ['foo.service'] },
    versions: { herdr: '1.2.0', pi: '0.9', autospec: '2.0', tailscale: '1.80', pi_extensions: { a: '1', b: '2' } },
    pins: { herdr: '1.2.0', pi: '1.0', pi_extensions: { a: '1', b: '9' } },
    fleet_update: { last_updated: '2026-10-20T03:00:00Z' },
    maintenance: false,
    problems: [], problem_count: 0,
    ...over,
  };
}
const neverReported = () => ({
  host_id: 'silent', host_name: 'silent', tailnet_ip: null, status: 'healthy', last_seen: iso(10_000),
  problems: [{ code: 'never_reported', severity: 'critical', message: 'Host has never reported' }], problem_count: 1,
});
const hist = [
  { ts: 1, at: iso(120_000), cpu: 10, mem: 50, disk: 70, gpu_util_pct: 80 },
  { ts: 2, at: iso(60_000), cpu: 30, mem: 55, disk: 71 },
  { ts: 3, at: iso(30_000), cpu: 42, mem: 61, disk: 71, gpu_util_pct: 88 },
];
const render = (h, history = hist, range = '1h') => renderHostDetail(h, history, { range, now: NOW });

test('test_never_reported_host_shows_no_telemetry_and_never_healthy', () => {
  const html = render(neverReported(), []);
  assert.match(html, /No telemetry reported/);
  assert.match(html, /Host has never reported/);
  assert.doesNotMatch(html, /Healthy/);
  assert.doesNotMatch(html, /stage="healthy"/i);
});

test('test_tailnet_ip_null_shows_not_reported', () => {
  const html = render(reportingHost({ tailnet_ip: null }));
  assert.match(html, /not reported/);
  assert.doesNotMatch(html, /\bnull\b/);
  assert.doesNotMatch(render(neverReported(), []), /\bnull\b/);
});

test('sparse host with nulls everywhere renders no null, undefined or NaN', () => {
  const h = reportingHost({
    tailnet_ip: null,
    updates: { manager: 'brew', outdated: 3, security: null, packages: [{ name: 'x', from: null, to: '2' }], reboot_required: null, failed_units: null },
    versions: { herdr: null, pi: '1', pi_extensions: { 'build-gate.ts': null } },
    pins: null, fleet_update: null,
  });
  h.latest.top.cpu[0].owner = null; h.latest.top.cpu[0].user = null;
  h.latest.gpus[0].temp_c = undefined;
  const html = render(h, [{ ts: 1, cpu: null }, { ts: 2, cpu: NaN }, 'junk', null]);
  assert.doesNotMatch(html, /\bnull\b|undefined|NaN/);
});

test('test_no_gpus_omits_gpu_section', () => {
  const h = reportingHost(); delete h.latest.gpus;
  assert.doesNotMatch(render(h), /data-section="gpu"|GPU/);
  assert.match(render(reportingHost()), /data-section="gpu"/);
  assert.match(render(reportingHost()), /RTX 4090/);
});

test('test_problems_strip_hidden_when_empty', () => {
  assert.doesNotMatch(render(reportingHost()), /data-section="problems"/);
});

test('test_problems_listed_with_severity', () => {
  const html = render(reportingHost({ problems: [
    { code: 'disk_critical', severity: 'critical', message: 'Disk 97% full on /' },
    { code: 'reboot_required', severity: 'warning', message: 'Reboot <required>' },
  ], problem_count: 2 }));
  assert.match(html, /data-section="problems"/);
  assert.match(html, /data-severity="critical"[^>]*>[\s\S]*?Disk 97% full on \//);
  assert.match(html, /data-severity="warning"[^>]*>[\s\S]*?Reboot &lt;required&gt;/);
});

test('test_sparkline_empty_and_single_point', () => {
  for (const pts of [[], [5], undefined, [NaN, null, 'x']]) {
    const svg = sparkline(pts, { width: 120, height: 30, max: 100 });
    assert.match(svg, /No history/);
    assert.doesNotMatch(svg, /NaN|<path/);
  }
  const ok = sparkline([1, 2, 3], { width: 120, height: 30, max: 100 });
  assert.match(ok, /^<svg/);
  assert.match(ok, /<path/);
  assert.doesNotMatch(ok, /NaN/);
  for (const pts of [[0, 0, 0], [5, 5], [0, NaN, 0, 3]]) assert.doesNotMatch(sparkline(pts, { width: 120, height: 30 }), /NaN|Infinity/);
});

test('test_host_name_is_escaped', () => {
  const evil = '<img src=x onerror=alert(1)>';
  const h = reportingHost({ host_name: evil, problems: [{ code: 'x', severity: 'warning', message: evil }] });
  h.latest.top.cpu[0].command = evil; h.latest.top.mem[0].user = evil; h.latest.mounts[1].path = evil;
  h.updates.packages[0].name = evil;
  const html = render(h);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
});

test('test_top_consumers_two_tables', () => {
  const html = render(reportingHost());
  assert.equal((html.match(/<table[^>]*data-top="(cpu|mem)"/g) || []).length, 2);
  assert.match(html, /llama-server/);
  assert.match(html, /service:llama/);
  assert.match(html, /wohlgemuth/);
});

test('test_updates_unknown_not_zero', () => {
  const unknown = render(reportingHost({ updates: { manager: null, outdated: null, security: null, packages: [] } }));
  assert.match(unknown, /data-field="outdated"[^>]*>unknown</);
  assert.doesNotMatch(unknown, /data-field="outdated"[^>]*>0</);
  const managerNull = render(reportingHost({ updates: { manager: null, outdated: 0, security: 0, packages: [] } }));
  assert.match(managerNull, /data-field="outdated"[^>]*>unknown</);
  const absent = render(reportingHost({ updates: undefined }));
  assert.match(absent, /not yet reported/);
  const known = render(reportingHost());
  assert.match(known, /data-field="outdated"[^>]*>12</);
  assert.match(known, /data-field="security"[^>]*>2</);
  assert.match(known, /Reboot required/);
  assert.match(known, /foo\.service/);
  assert.match(known, /<details[\s\S]*openssl/);
});

test('formatUptime(273600) === "3d 4h"', () => {
  assert.equal(formatUptime(273600), '3d 4h');
  assert.equal(formatUptime(5 * 3600 + 12 * 60), '5h 12m');
  assert.equal(formatUptime(7 * 60 + 5), '7m');
  for (const bad of [null, undefined, -1, NaN, 'x']) assert.equal(formatUptime(bad), '—');
});

test('formatBytes scales binary units and rejects junk', () => {
  assert.equal(formatBytes(512), '512 B');
  assert.equal(formatBytes(24 * 2 ** 30), '24.0 GiB');
  assert.equal(formatBytes(1.5 * 2 ** 40), '1.50 TiB');
  for (const bad of [null, undefined, -1, NaN]) assert.equal(formatBytes(bad), '—');
});

test('pins drift marker on differing versions including pi extensions', () => {
  const html = render(reportingHost());
  assert.match(html, /data-version="pi"[^>]*data-drift="true"/);
  assert.match(html, /data-version="herdr"[^>]*data-drift="false"/);
  assert.match(html, /data-version="pi\/b"[^>]*data-drift="true"/);
  assert.match(html, /data-version="pi\/a"[^>]*data-drift="false"/);
  assert.match(html, /data-version="tailscale"[^>]*data-drift="false"/);
  assert.match(html, /Last fleet update/);
});

test('history accepts an array or {samples}; other shapes are empty', () => {
  const fromArray = render(reportingHost(), hist);
  const fromObject = render(reportingHost(), { samples: hist, range: '1h' });
  assert.equal(fromArray, fromObject);
  assert.match(fromArray, /data-series="cpu"[\s\S]*?<path/);
  for (const bad of [null, undefined, 'x', { samples: 'x' }, 42]) {
    const html = renderHostDetail(reportingHost(), bad, { range: '1h', now: NOW });
    assert.match(html, /No history/);
    assert.doesNotMatch(html, /NaN/);
  }
});

test('stale host is not healthy', () => {
  const h = reportingHost({ last_seen: iso(3 * 3600_000) });
  h.latest.at = iso(3 * 3600_000);
  const html = render(h);
  assert.match(html, /stage="stalled"/);
  assert.doesNotMatch(html, /stage="healthy"/i);
});

test('critical problem overrides a healthy status; current host shows its status', () => {
  assert.match(render(reportingHost()), /stage="healthy"/);
  const html = render(reportingHost({ problems: [{ code: 'disk_critical', severity: 'critical', message: 'Disk full' }] }));
  assert.match(html, /stage="critical"/);
});

test('header shows uptime, agents, maintenance and range picker', () => {
  const html = render(reportingHost({ maintenance: true }), hist, '24h');
  assert.match(html, /3d 4h/);
  assert.match(html, /3 agents running/);
  assert.match(html, /Maintenance/);
  assert.match(html, /href="#\/hosts\/beast\?range=24h"[^>]*aria-current="true"/);
  assert.match(html, /href="#\/hosts\/beast\?range=7d"/);
  const h = reportingHost(); delete h.latest.agents_running;
  assert.doesNotMatch(render(h), /agents running/);
});

// ---- Task 8: list cards ----
import { renderHostCard } from '../host-view.js';

test('card shows cpu, memory, disk and gpu meters from latest', () => {
  const html = renderHostCard(reportingHost(), { now: NOW });
  for (const k of ['cpu', 'mem', 'disk', 'gpu']) assert.match(html, new RegExp(`data-meter="${k}"`));
  assert.match(html, /width:88%/);
  assert.match(html, /<herdr-status stage="healthy">/);
  assert.match(html, /<code>100\.64\.0\.7<\/code>/);
  assert.doesNotMatch(html, /hv-cardproblem/);
});

test('gpu meter absent without gpus', () => {
  const h = reportingHost(); h.latest.gpus = [];
  assert.doesNotMatch(renderHostCard(h, { now: NOW }), /data-meter="gpu"/);
});

test('problem badge shows count and most severe message; none at zero', () => {
  const h = reportingHost({ problem_count: 2, problems: [
    { severity: 'warning', code: 'a', message: 'disk <b>high</b>' },
    { severity: 'critical', code: 'b', message: 'gpu lost' }] });
  const html = renderHostCard(h, { now: NOW });
  assert.match(html, /data-problem-badge[^>]*data-severity="critical"/);
  assert.match(html, /title="gpu lost"/);
  assert.match(html, />2 problems</);
  assert.doesNotMatch(html, /<b>/);
  assert.doesNotMatch(renderHostCard(reportingHost(), { now: NOW }), /data-problem-badge/);
});

test('stale host renders stalled; no latest renders no telemetry, never healthy', () => {
  const stale = reportingHost({ last_seen: iso(3_600_000) }); stale.latest.at = iso(3_600_000);
  assert.match(renderHostCard(stale, { now: NOW }), /stage="stalled"/);
  const none = renderHostCard({ host_id: 'x', host_name: 'x', status: 'healthy', last_seen: iso(1000), latest: null }, { now: NOW });
  assert.match(none, /No telemetry/);
  assert.doesNotMatch(none, /stage="healthy"/);
  assert.doesNotMatch(none, /data-meter/);
});

test('card escapes name, shows not reported for null tailnet, clamps bad values', () => {
  const h = reportingHost({ host_name: '<img src=x onerror=1>', tailnet_ip: null });
  h.latest.cpu = 250; h.latest.mem = NaN; h.latest.disk = -5; h.latest.gpus = [{ util_pct: Infinity }];
  const html = renderHostCard(h, { now: NOW });
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /not reported/);
  assert.doesNotMatch(html, /null|NaN|Infinity/);
  assert.match(html, /width:100%/);
  assert.match(html, /width:0%/);
});

import { HOST_CARD_STYLE } from '../host-view.js';

test('list page style carries the shared badge rules', () => {
  assert.match(HOST_CARD_STYLE, /\.hv-badge\{/);
  assert.match(HOST_CARD_STYLE, /\.hv-badge--crit/);
});

test('recently seen host without latest is not "no report since"; wording matches detail', () => {
  const host = { host_id: 'x', host_name: 'x', last_seen: iso(1000), latest: null };
  const card = renderHostCard(host, { now: NOW });
  assert.doesNotMatch(card, /no report since/);
  assert.match(card, /registered, no telemetry/);
  assert.match(card, /No telemetry reported/);
  assert.match(renderHostDetail(host, [], { now: NOW }), /No telemetry reported/);
  const stale = reportingHost({ last_seen: iso(3_600_000) }); stale.latest.at = iso(3_600_000);
  assert.match(renderHostCard(stale, { now: NOW }), /no report since/);
});
