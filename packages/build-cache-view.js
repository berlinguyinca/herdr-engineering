// Build cache page (micro-spec 10): a read-only view of GET /api/v1/build-cache/{status,hosts,builds,builds/{id}} plus one
// operator-only dry run, POST /api/v1/build-cache/cleanup/preview. Applying a cleanup is CLI-only; this page has no apply control.
//
// Honesty rules, enforced here and by tests/playwright/build-cache.test.mjs:
//   * hit/miss rates are computed only from the sccache counters a build recorded; with none, the page says "unavailable" and why.
//     Nothing is estimated and no saving is shown.
//   * the two write figures are different things and are labelled as such: proc_io_write_bytes is a sampled LOGICAL lower bound for
//     the build's own processes; host_disk_write_bytes_delta is the whole device's delta during the build (other work included).
//   * every string from the server is untrusted: it is escaped with esc() before it enters markup, never interpolated raw.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { skeleton } from './motion.js';
import { morph } from './patch.js';
import { pollWhileVisible } from './refresh-bus.js';

export const POLL_MS = 30000;
export const PAGE_SIZE = 50;
const MAX_CANDIDATES_SHOWN = 50;
export const CLI_APPLY = 'herdr-eng build-cache-evict --apply';
const BASE = '/api/v1/build-cache';

const isObject = (v) => v != null && typeof v === 'object' && !Array.isArray(v);
const list = (v) => (Array.isArray(v) ? v : []);
const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const text = (v, fallback = '—') => (v == null || v === '' ? fallback : String(v));

export function bytes(v) {
  const n = num(v);
  if (n == null) return null;
  if (Math.abs(n) < 1024) return `${n} B`;
  const units = ['KiB', 'MiB', 'GiB', 'TiB'];
  let x = n / 1024, i = 0;
  while (Math.abs(x) >= 1024 && i < units.length - 1) { x /= 1024; i += 1; }
  return `${x.toFixed(x >= 100 ? 0 : 1)} ${units[i]}`;
}
const seconds = (v) => (num(v) == null ? null : v < 60 ? `${v.toFixed(1)} s` : `${Math.floor(v / 60)} m ${Math.round(v % 60)} s`);
const unavailable = (why) => `<span class="bc-na" data-bc-unavailable>unavailable<span class="visually-hidden">: ${esc(why)}</span></span>`;

// ---------------------------------------------------------------- measured figures (pure)

const HITS = /^cache_hits\.counts\./;
const MISSES = /^cache_misses\.counts\./;

/** Hit/miss from the builds' recorded sccache deltas. `rate` is null (with a `reason`) unless something was measured. */
export function cacheCounts(builds) {
  const rows = list(builds).filter(isObject);
  let hits = 0, misses = 0, measured = 0, noStats = 0;
  for (const b of rows) {
    if (b.status === 'running_or_unknown') continue;
    const stats = isObject(b.sccache_stats) ? b.sccache_stats : null;
    if (!stats) { if (list(b.unavailable).includes('sccache_stats')) noStats += 1; continue; }
    let h = 0, m = 0, seen = false;
    for (const [k, v] of Object.entries(stats)) {
      if (num(v) == null) continue;
      if (HITS.test(k)) { h += v; seen = true; } else if (MISSES.test(k)) { m += v; seen = true; }
    }
    if (seen) { hits += h; misses += m; measured += 1; }
  }
  const total = hits + misses;
  if (measured && total > 0) return { hits, misses, measured, builds: rows.length, rate: hits / total, reason: null };
  let reason;
  if (!rows.length) reason = 'no builds are recorded yet';
  else if (noStats) reason = `sccache statistics were unavailable for ${noStats} of ${rows.length} loaded builds (sccache missing or its stats could not be read)`;
  else reason = `none of the ${rows.length} loaded builds recorded sccache hit or miss counters (cache not applied, or nothing was compiled)`;
  return { hits, misses, measured, builds: rows.length, rate: null, reason };
}

/** Sums of the two write figures over the builds that measured each; never counts a missing value as zero. */
export function writeTotals(builds) {
  const finished = list(builds).filter((b) => isObject(b) && b.status !== 'running_or_unknown');
  const sum = (key) => {
    let total = 0, n = 0;
    for (const b of finished) { const v = num(b[key]); if (v != null) { total += v; n += 1; } }
    return { total: n ? total : null, measured: n, of: finished.length };
  };
  return { logical: sum('proc_io_write_bytes'), disk: sum('host_disk_write_bytes_delta') };
}

// ---------------------------------------------------------------- rendering (pure)

export const BUILD_CACHE_STYLE = `<style>
/* The theme's light accent (#10a37f) is 3.2:1 on white, below WCAG AA for links and button text; this page uses the darker accent the collaboration styles already use. */
.bc-root{--accent:#0a7358;--accent-hover:#085f49}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]) .bc-root{--accent:#19c39b;--accent-hover:#14a883}}
:root[data-theme=dark] .bc-root{--accent:#19c39b;--accent-hover:#14a883}
.bc-root .hd-btn:focus-visible{outline:3px solid var(--text);outline-offset:2px}
.bc-note{font-size:.8125rem;color:var(--text-secondary);margin:4px 0 12px;max-width:72ch}
.bc-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,15rem),1fr));gap:12px;margin:12px 0}
.bc-card{background:var(--surface);border:1px solid var(--border-subtle);border-radius:var(--radius);padding:12px;margin:0}
.bc-card dt{font-size:.75rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--text-secondary)}
.bc-card dd{margin:4px 0 0;font-size:1.25rem;font-weight:600;overflow-wrap:anywhere}
.bc-card p{margin:4px 0 0;font-size:.8125rem;font-weight:400;color:var(--text-secondary);overflow-wrap:anywhere}
.bc-na{color:var(--text-secondary);font-style:italic;font-weight:400}
.bc-meter{height:8px;border-radius:4px;background:var(--surface-2);overflow:hidden;margin-top:8px}.bc-meter i{display:block;height:100%;background:var(--accent)}
.bc-badge{display:inline-block;border:2px solid var(--border);border-radius:4px;padding:0 4px;font-size:.75rem;font-weight:600;color:var(--text)}
.bc-badge[data-status=completed]{border-color:var(--status-ok)}.bc-badge[data-status=failed]{border-color:var(--status-failed)}.bc-badge[data-status=running_or_unknown]{border-color:var(--status-attention)}
.bc-wrap th,.bc-wrap td{color:var(--text)}.bc-wrap code,.bc-dl code,.bc-note code,.bc-card code,.bc-notice code{color:var(--text)}
.bc-wrap{overflow-x:auto;max-width:100%;position:relative}
.bc-wrap table{min-width:56rem}
.bc-wrap :is(th,td){overflow-wrap:anywhere}
.bc-wrap td.bc-num{text-align:right;white-space:nowrap}
.bc-notice{border:1px solid var(--border);border-radius:var(--radius);padding:12px;margin:12px 0;background:var(--surface)}
.bc-notice[data-kind=error]{border-color:var(--status-failed)}
.bc-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0}
.bc-dl{display:grid;grid-template-columns:minmax(8rem,14rem) 1fr;gap:4px 12px}.bc-dl dd{margin:0;overflow-wrap:anywhere}
@media (max-width:40rem){.bc-dl{grid-template-columns:1fr}.bc-dl dd{margin-bottom:8px}}
</style>`;

const card = (label, valueHtml, noteHtml = '') => `<div class="bc-card"><dt>${esc(label)}</dt><dd>${valueHtml}</dd>${noteHtml ? `<p>${noteHtml}</p>` : ''}</div>`;

export const buildHref = (id) => `#/build-cache/${encodeURIComponent(id)}`;
const sessionLink = (id) => `<a href="#/herd/session/${encodeURIComponent(id)}">session <code>${esc(id)}</code></a>`;
const missionLink = (id) => `<a href="#/missions/${encodeURIComponent(id)}">mission <code>${esc(id)}</code></a>`;
function traceCell(trace) {
  if (!isObject(trace)) return '—';
  const parts = [];
  if (typeof trace.session === 'string' && trace.session) parts.push(sessionLink(trace.session));
  if (typeof trace.mission === 'string' && trace.mission) parts.push(missionLink(trace.mission));
  return parts.join('<br>') || '—';
}
const writeCell = (v) => (bytes(v) == null ? unavailable('not measured for this build') : esc(bytes(v)));
const statusBadge = (s) => `<span class="bc-badge" data-status="${esc(s)}">${esc(String(s).replace(/_/g, ' '))}</span>`;
const cacheAppliedCell = (b) => (isObject(b.cache) ? (b.cache.applied ? 'applied' : 'not applied') : '—');

function hostsSection(hostsRes) {
  if (hostsRes.error) return `<section aria-labelledby="bc-h-hosts"><h2 id="bc-h-hosts">Host cache size</h2><p class="bc-notice" data-kind="error" role="note" data-bc-section-error>Host cache size could not load: ${esc(hostsRes.error)}</p></section>`;
  const hosts = list(hostsRes.data?.hosts).filter(isObject);
  const body = hosts.length ? hosts.map((h) => {
    const c = isObject(h.cache) ? h.cache : {};
    const used = num(c.used_bytes), cap = num(c.size_cap_gib);
    const capBytes = cap == null ? null : cap * 1024 ** 3;
    const pct = used != null && capBytes ? Math.min(100, Math.round((used / capBytes) * 1000) / 10) : null;
    const usedText = used == null ? unavailable('the cache directory could not be measured') : `${c.used_truncated ? 'at least ' : ''}${esc(bytes(used))}`;
    return `<div class="bc-card" data-bc-host="${esc(text(h.host_id))}">
      <dt>${esc(text(h.host_id, 'unknown host'))}</dt><dd>${usedText}${capBytes ? ` <span class="bc-na">of ${esc(bytes(capBytes))}</span>` : ''}</dd>
      ${pct != null ? `<div class="bc-meter" role="meter" aria-label="Cache used on ${esc(text(h.host_id))}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><i style="width:${pct}%"></i></div>` : ''}
      <p>${num(c.free_disk_bytes) == null ? 'free disk unavailable' : `${esc(bytes(c.free_disk_bytes))} free on disk`}${c.used_truncated ? ' · the size walk hit its file limit' : ''}</p>
      <p>sccache ${h.sccache_present ? esc(text(h.sccache_version, 'present')) : 'not installed'} · ${esc(text(h.os))}/${esc(text(h.arch))}</p>
    </div>`;
  }).join('') : '';
  return `<section aria-labelledby="bc-h-hosts"><h2 id="bc-h-hosts">Host cache size</h2>
    <p class="bc-note">Only the control-plane host is listed${hostsRes.data?.source ? ` (source: ${esc(hostsRes.data.source)})` : ''}. Other build hosts do not report here.</p>
    ${hosts.length ? `<dl class="bc-cards">${body}</dl>` : '<herdr-empty data-bc-hosts-empty message="No host cache information"></herdr-empty>'}</section>`;
}

function buildsTable(builds, hasMore) {
  if (!builds.length) return '<herdr-empty data-bc-empty message="No builds recorded yet"></herdr-empty><p class="bc-note">Builds appear here once a gated build runs on the control-plane host.</p>';
  const rows = builds.map((b) => `<tr data-bc-build="${esc(b.build_id)}">
      <th scope="row"><a href="${buildHref(b.build_id)}"><code>${esc(b.build_id)}</code></a></th>
      <td>${statusBadge(text(b.status, 'unknown'))}</td><td>${esc(text(b.cmd_kind))}</td><td>${esc(text(b.host))}</td>
      <td>${esc(text(b.ts))}</td><td class="bc-num">${seconds(b.elapsed_s) == null ? '—' : esc(seconds(b.elapsed_s))}</td>
      <td>${esc(cacheAppliedCell(b))}</td><td class="bc-num">${writeCell(b.proc_io_write_bytes)}</td><td class="bc-num">${writeCell(b.host_disk_write_bytes_delta)}</td>
      <td>${traceCell(b.trace)}</td></tr>`).join('');
  return `<div class="bc-wrap" role="region" aria-label="Builds table" tabindex="0"><table class="hd-table" data-bc-builds>
    <caption class="visually-hidden">Recorded builds, newest first</caption>
    <thead><tr><th scope="col">Build</th><th scope="col">Status</th><th scope="col">Command</th><th scope="col">Host</th><th scope="col">Time</th><th scope="col">Elapsed</th><th scope="col">Cache</th><th scope="col">Logical writes (lower bound)</th><th scope="col">Host disk delta</th><th scope="col">Trace</th></tr></thead>
    <tbody>${rows}</tbody></table></div>
    <p class="bc-note" data-bc-count>${builds.length} build${builds.length === 1 ? '' : 's'} loaded${hasMore ? ' · more are available' : ' · end of list'}.</p>`;
}

/** The whole page body from already-fetched data: { status, hosts, builds } each `{ data } | { error }`, plus { extra, more }. */
export function renderBuildCache(model) {
  const st = model.status;
  const status = isObject(st.data) ? st.data : {};
  const summary = isObject(status.summary) ? status.summary : null;
  const builds = model.builds;
  const counts = cacheCounts(builds);
  const writes = writeTotals(builds);
  const notes = [];
  if (st.error) notes.push(`<p class="bc-notice" data-kind="error" role="note" data-bc-section-error>Cache status could not load: ${esc(st.error)}</p>`);
  else if (status.policy_enabled === false) notes.push('<p class="bc-notice" role="note" data-bc-degraded>The cache policy is disabled on this host (fabric_build_cache.enabled is false), so builds run without the shared cache and no size policy applies. Recorded builds are still listed.</p>');
  if (!st.error && status.log_present === false) notes.push('<p class="bc-notice" role="note" data-bc-degraded>No build log exists on the control-plane host yet. Figures stay unavailable until a gated build writes one.</p>');
  if (model.builds_error) notes.push(`<p class="bc-notice" data-kind="error" role="note" data-bc-section-error>Builds could not load: ${esc(model.builds_error)}</p>`);

  const rate = counts.rate == null
    ? card('Cache hit rate', unavailable(counts.reason), esc(counts.reason))
    : card('Cache hit rate', `${(counts.rate * 100).toFixed(1)}%`, `${counts.hits} hits, ${counts.misses} misses · measured by sccache in ${counts.measured} of ${counts.builds} loaded builds`);
  const logical = writes.logical.total == null
    ? card('Logical writes (lower bound)', unavailable('no loaded build measured it'), 'Sampled from /proc/&lt;pid&gt;/io for the build\'s own processes.')
    : card('Logical writes (lower bound)', esc(bytes(writes.logical.total)), `Sampled per-process write calls; a lower bound, not disk writes. Measured in ${writes.logical.measured} of ${writes.logical.of} finished builds.`);
  const disk = writes.disk.total == null
    ? card('Host disk delta', unavailable('no loaded build measured it'), 'Whole-device sector delta during each build.')
    : card('Host disk delta', esc(bytes(writes.disk.total)), `Everything written to the device during each build, other work included; overlapping builds can count the same writes twice. Measured in ${writes.disk.measured} of ${writes.disk.of} finished builds.`);
  const totals = summary
    ? card('Builds recorded', esc(String(num(summary.builds) ?? '—')), `${esc(String(num(summary.failed) ?? 0))} failed · the whole log, not just the loaded page`)
    : card('Builds recorded', unavailable('the status endpoint returned no summary'));

  return `${notes.join('')}
    <p class="bc-note" data-bc-scope>Read-only. Figures come from this control-plane host's own build records; hit rate and writes cover the ${builds.length} loaded build${builds.length === 1 ? '' : 's'}. Nothing here is estimated.</p>
    <section aria-labelledby="bc-h-summary"><h2 id="bc-h-summary">Measured cache and write figures</h2>
      <dl class="bc-cards" data-bc-cards>${totals}${rate}${logical}${disk}</dl></section>
    ${hostsSection(model.hosts)}
    <section aria-labelledby="bc-h-builds"><h2 id="bc-h-builds">Builds</h2>${buildsTable(builds, model.more)}</section>`;
}

const DETAIL_FIELDS = [
  ['status', 'Status'], ['cmd_kind', 'Command'], ['host', 'Host'], ['slot', 'Slot'], ['ts', 'Finished at'], ['exit_code', 'Exit code'],
  ['elapsed_s', 'Elapsed (s)'], ['user_cpu_s', 'User CPU (s)'], ['sys_cpu_s', 'System CPU (s)'], ['max_rss_kb', 'Peak memory (KiB)'],
  ['rusage_oublock', 'Block writes (rusage_oublock)'], ['host_disk_scope', 'Host disk scope'],
];

export function renderBuildDetail(b) {
  const row = (label, html) => `<dt>${esc(label)}</dt><dd>${html}</dd>`;
  const cells = DETAIL_FIELDS.filter(([k]) => b[k] != null && b[k] !== '').map(([k, label]) => row(label, esc(String(b[k]))));
  cells.push(row('Logical writes (lower bound)', writeCell(b.proc_io_write_bytes)));
  cells.push(row('Host disk delta', writeCell(b.host_disk_write_bytes_delta)));
  cells.push(row('Cache', esc(cacheAppliedCell(b)) + (isObject(b.cache) && list(b.cache.reasons).length ? ` (${list(b.cache.reasons).map((r) => esc(r)).join(', ')})` : '')));
  const counts = cacheCounts([{ ...b, status: b.status === 'running_or_unknown' ? b.status : 'completed' }]);
  cells.push(row('Cache hit rate', counts.rate == null ? unavailable(counts.reason) : `${(counts.rate * 100).toFixed(1)}% (${counts.hits} hits, ${counts.misses} misses)`));
  cells.push(row('Session', isObject(b.trace) && b.trace.session ? sessionLink(b.trace.session) : '—'));
  cells.push(row('Mission', isObject(b.trace) && b.trace.mission ? missionLink(b.trace.mission) : '—'));
  if (list(b.unavailable).length) cells.push(row('Unavailable measurements', list(b.unavailable).map((u) => `<code>${esc(u)}</code>`).join(', ')));
  const stats = isObject(b.sccache_stats) ? Object.entries(b.sccache_stats) : [];
  return `<p><a href="#/build-cache">Back to build cache</a></p>
    <h2 data-bc-detail-id>Build <code>${esc(b.build_id)}</code></h2>
    <dl class="bc-dl" data-bc-detail>${cells.join('')}</dl>
    ${stats.length ? `<h3>sccache counters (change during this build)</h3><div class="bc-wrap" role="region" aria-label="sccache counters" tabindex="0"><table class="hd-table"><thead><tr><th scope="col">Counter</th><th scope="col">Change</th></tr></thead><tbody>${stats.map(([k, v]) => `<tr><th scope="row"><code>${esc(k)}</code></th><td class="bc-num">${esc(String(v))}</td></tr>`).join('')}</tbody></table></div>` : ''}`;
}

/** The dry-run plan returned by POST cleanup/preview. */
export function renderPreview(res) {
  const plan = isObject(res?.plan) ? res.plan : null;
  const apply = `<p class="bc-note" data-bc-cli-only><strong>Preview only. Nothing was deleted.</strong> Applying a cleanup is CLI-only: run <code>${esc(CLI_APPLY)}</code> on the build host. This page cannot apply it.</p>`;
  const head = `<p data-bc-preview-status>Status: <strong>${esc(text(res?.status, 'unknown'))}</strong>${res?.reason ? ` · reason: ${esc(res.reason)}` : ''}${isObject(res?.admission) ? ` · admission: ${res.admission.allowed ? 'allowed' : 'refused'} (${esc(text(res.admission.reason))})` : ''}${res?.plan_id ? ` · plan <code>${esc(res.plan_id)}</code>` : ''}</p>`;
  if (!plan) return `${head}<p data-bc-preview-empty>No eviction plan: ${esc(text(res?.reason, 'nothing to plan'))}.</p>${apply}`;
  const cands = list(plan.candidates).filter(isObject);
  const shown = cands.slice(0, MAX_CANDIDATES_SHOWN);
  return `${head}
    <dl class="bc-dl" data-bc-plan><dt>Planned to free</dt><dd>${esc(bytes(plan.planned_bytes) ?? '—')} in ${esc(text(plan.planned_count, '0'))} entries${plan.truncated ? ' (the plan was truncated at its per-run limit)' : ''}</dd>
      <dt>Cache used</dt><dd>${esc(bytes(plan.used_bytes) ?? '—')} of ${esc(bytes(plan.capacity_bytes) ?? '—')} (hard cap ${esc(bytes(plan.hard_cap_bytes) ?? '—')})</dd>
      <dt>Skipped (in use or too recent)</dt><dd>${esc(text(plan.skipped_count, '0'))}</dd></dl>
    ${shown.length ? `<div class="bc-wrap" role="region" aria-label="Cleanup candidates" tabindex="0"><table class="hd-table" data-bc-candidates><caption class="visually-hidden">Entries the cleanup would remove</caption><thead><tr><th scope="col">Candidate</th><th scope="col">Size</th><th scope="col">Reason</th></tr></thead><tbody>${shown.map((c) => `<tr><th scope="row"><code>${esc(c.id)}</code></th><td class="bc-num">${esc(bytes(c.bytes) ?? '—')}</td><td>${esc(text(c.reason))}</td></tr>`).join('')}</tbody></table></div>${cands.length > shown.length ? `<p class="bc-note">Showing ${shown.length} of ${cands.length} candidates.</p>` : ''}` : '<p data-bc-preview-empty>The plan has no candidates: nothing would be removed.</p>'}
    ${apply}`;
}

// ---------------------------------------------------------------- loading

const failure = (error) => (error?.message ? String(error.message) : 'request failed');
const settle = async (promise) => { try { return { data: await promise }; } catch (error) { return { error: failure(error), status: error?.status ?? 0 }; } };
const buildsQuery = (cursor) => `${BASE}/builds?limit=${PAGE_SIZE}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`;

export async function loadBuildCache(client, { cursor = null } = {}) {
  const [status, hosts, builds] = await Promise.all([
    settle(client.request(`${BASE}/status`)), settle(client.request(`${BASE}/hosts`)), settle(client.request(buildsQuery(cursor))),
  ]);
  return { status, hosts, builds };
}

/** Why a whole-page failure happened, in words an operator can act on (null: not a page-level failure). */
export function pageFailure(res) {
  const s = res.status;
  if (!s.error) return null;
  if (s.status === 401) return { kind: 'auth', message: 'Sign-in required: the control plane did not accept your credentials (401). Build cache data is not shown.' };
  if (s.status === 403) return { kind: 'denied', message: 'Permission denied (403): your role cannot read build cache data.' };
  if (s.status === 404 || s.status === 501) return { kind: 'disabled', message: 'The build cache API is not enabled on this control plane.' };
  if (res.builds.error && res.hosts.error) return { kind: 'error', message: `Build cache could not load: ${s.error}` };
  return null;
}

export function previewFailure(error) {
  if (error?.status === 401) return 'Sign-in required (401): the preview was not run.';
  if (error?.status === 403) return 'Permission denied (403): only operators can preview a cleanup. Nothing was run.';
  if (error?.status === 404) return 'The build cache API is not enabled on this control plane (404).';
  if (error?.status === 501) return `Not available (501): apply is CLI-only (${CLI_APPLY}).`;
  return `The preview failed: ${failure(error)}`;
}

// ---------------------------------------------------------------- mounting

export function mountBuildCache(container, client, { freshness = () => null } = {}) {
  let disposed = false, controller = null, first = null, extra = [], cursor = null, loaded = false, signature = null, busy = false;
  container.classList.add('bc-root');
  container.innerHTML = `${BUILD_CACHE_STYLE}
    <p class="bc-note">Build cache measurements for builds run through the build gate. Read-only: the one action here is a dry-run preview.</p>
    <p data-bc-status role="status"></p>
    <div data-bc-content></div>
    <div class="bc-actions"><button type="button" class="hd-btn" data-bc-more hidden>Load more builds</button><button type="button" class="hd-btn" data-bc-retry hidden>Retry</button></div>
    <section aria-labelledby="bc-h-cleanup"><h2 id="bc-h-cleanup">Cleanup preview</h2>
      <p class="bc-note">Shows which cache entries a cleanup would remove under the current policy. It never deletes anything, and it is restricted to operators. Applying a cleanup is CLI-only: <code>${esc(CLI_APPLY)}</code>.</p>
      <div class="bc-actions"><button type="button" class="hd-btn" data-bc-preview aria-describedby="bc-preview-help">Preview cleanup (dry run)</button><span id="bc-preview-help" class="bc-note" style="margin:0">Operator role required.</span></div>
      <div data-bc-preview-out role="region" aria-live="polite" aria-label="Cleanup preview result" tabindex="-1"></div></section>`;
  const content = container.querySelector('[data-bc-content]');
  const status = container.querySelector('[data-bc-status]');
  const retry = container.querySelector('[data-bc-retry]');
  const more = container.querySelector('[data-bc-more]');
  const previewBtn = container.querySelector('[data-bc-preview]');
  const out = container.querySelector('[data-bc-preview-out]');

  const allBuilds = () => {
    const seen = new Set(), rows = [];
    for (const b of [...(first?.data?.builds ? list(first.data.builds) : []), ...extra]) {
      if (isObject(b) && typeof b.build_id === 'string' && !seen.has(b.build_id)) { seen.add(b.build_id); rows.push(b); }
    }
    return rows;
  };
  const paint = (res) => {
    const next = JSON.stringify([res, extra, cursor]);
    if (next === signature) return;
    signature = next;
    const html = renderBuildCache({ status: res.status, hosts: res.hosts, builds: allBuilds(), builds_error: res.builds.error || null, more: cursor != null });
    morph(content, `<div>${html}</div>`);
    more.hidden = cursor == null;
  };

  const refresh = async () => {
    if (disposed || controller) return;
    const request = controller = new AbortController();
    retry.hidden = true;
    if (!loaded) content.innerHTML = skeleton('build cache', { rows: 3, height: 120 });
    try {
      const res = await loadBuildCache(client);
      if (disposed || request.signal.aborted) return;
      const fail = pageFailure(res);
      if (fail) {
        loaded = false; signature = null; content.innerHTML = ''; more.hidden = true;
        status.textContent = fail.message;
        status.setAttribute('role', 'alert');
        // Auth failures are not retried by polling noise: the button is the explicit way to try again.
        retry.hidden = false; previewBtn.disabled = fail.kind === 'disabled';
        freshness()?.touch(false, fail.message);
        return;
      }
      first = res.builds; if (!extra.length) cursor = res.builds.data?.next_cursor ?? null;
      status.textContent = ''; status.setAttribute('role', 'status'); previewBtn.disabled = false;
      loaded = true; paint(res); freshness()?.touch(true);
    } catch (error) {
      if (!disposed && !request.signal.aborted) { status.textContent = `Build cache could not load: ${failure(error)}`; status.setAttribute('role', 'alert'); retry.hidden = false; freshness()?.touch(false, failure(error)); }
    } finally { if (controller === request) controller = null; }
  };

  const loadMore = async () => {
    if (busy || cursor == null) return;
    busy = true; more.disabled = true; more.setAttribute('aria-busy', 'true');
    try {
      const page = await client.request(buildsQuery(cursor));
      extra = extra.concat(list(page?.builds)); cursor = page?.next_cursor ?? null; signature = null;
      const res = await loadBuildCache(client);   // repaint with fresh status; the extra pages stay
      if (!disposed) { first = res.builds; paint(res); status.textContent = `Loaded ${allBuilds().length} builds.`; }
    } catch (error) { status.textContent = `More builds could not load: ${failure(error)}`; status.setAttribute('role', 'alert'); }
    finally { busy = false; more.disabled = false; more.removeAttribute('aria-busy'); }
  };

  const preview = async () => {
    if (previewBtn.disabled) return;
    previewBtn.disabled = true; previewBtn.setAttribute('aria-busy', 'true');
    out.innerHTML = '<p role="status">Running the dry run…</p>';
    try {
      const res = await client.request(`${BASE}/cleanup/preview`, { method: 'POST', body: {} });
      if (disposed) return;
      out.innerHTML = renderPreview(res);
    } catch (error) {
      if (!disposed) out.innerHTML = `<p class="bc-notice" data-kind="error" role="alert" data-bc-preview-error>${esc(previewFailure(error))}</p><p class="bc-note" data-bc-cli-only>Applying a cleanup is CLI-only: <code>${esc(CLI_APPLY)}</code>.</p>`;
    } finally { if (!disposed) { previewBtn.disabled = false; previewBtn.removeAttribute('aria-busy'); out.focus({ preventScroll: true }); } }
  };

  retry.addEventListener('click', refresh);
  more.addEventListener('click', loadMore);
  previewBtn.addEventListener('click', preview);
  const stop = pollWhileVisible(refresh, POLL_MS);
  void refresh();
  return { refresh, dispose() { disposed = true; stop(); controller?.abort(); } };
}

export function mountBuildDetail(container, client, id, { freshness = () => null } = {}) {
  let disposed = false, controller = null, signature = null;
  container.classList.add('bc-root');
  container.innerHTML = `${BUILD_CACHE_STYLE}<p data-bc-status role="status"></p><div data-bc-content></div><button type="button" class="hd-btn" data-bc-retry hidden>Retry</button>`;
  const content = container.querySelector('[data-bc-content]');
  const status = container.querySelector('[data-bc-status]');
  const retry = container.querySelector('[data-bc-retry]');
  const refresh = async () => {
    if (disposed || controller) return;
    const request = controller = new AbortController();
    retry.hidden = true;
    if (signature == null) content.innerHTML = skeleton('build', { rows: 2, height: 48 });
    try {
      const b = await client.request(`${BASE}/builds/${encodeURIComponent(id)}`);
      if (disposed || request.signal.aborted) return;
      if (!isObject(b) || typeof b.build_id !== 'string') throw new Error('Invalid build response');
      const next = JSON.stringify(b);
      if (next !== signature) { signature = next; morph(content, `<div>${renderBuildDetail(b)}</div>`); }
      status.textContent = ''; freshness()?.touch(true);
    } catch (error) {
      if (disposed || request.signal.aborted) return;
      content.innerHTML = '';
      signature = null;
      const message = error?.status === 404
        ? 'Build not found. It may have rotated out of the log window.'
        : error?.status === 401 ? 'Sign-in required (401).'
          : error?.status === 403 ? 'Permission denied (403): your role cannot read build cache data.'
            : `Build could not load: ${failure(error)}`;
      status.textContent = message; status.setAttribute('role', 'alert');
      content.innerHTML = '<p><a href="#/build-cache">Back to build cache</a></p>';
      retry.hidden = error?.status === 404; freshness()?.touch(error?.status === 404, message);
    } finally { if (controller === request) controller = null; }
  };
  retry.addEventListener('click', refresh);
  const stop = pollWhileVisible(refresh, POLL_MS);
  void refresh();
  return { refresh, dispose() { disposed = true; stop(); controller?.abort(); } };
}
