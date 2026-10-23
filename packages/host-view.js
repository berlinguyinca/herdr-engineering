// Host detail page: a pure render of one host record plus its telemetry history.
// DOM-free so it is node-testable. Every dynamic string goes through `show`/`esc`;
// a missing value renders as an explicit placeholder, never "null" or "0".
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { formatTimestamp } from './herdr-web-components/format.js';
import { reportFreshness, reportTimestamp } from './record-state.js';

export const RANGES = ['1h', '24h', '7d'];
const PINNED = ['herdr', 'pi', 'autospec'];
const VERSION_KEYS = ['herdr', 'pi', 'autospec', 'tailscale'];

const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const obj = (v) => (v && typeof v === 'object' && !Array.isArray(v) ? v : null);
const list = (v) => (Array.isArray(v) ? v : []);
const show = (v, fallback = '—') => (v == null || (typeof v === 'number' && !Number.isFinite(v)) || v === '' ? fallback : esc(v));
const round1 = (v) => Math.round(v * 10) / 10;
const pct = (v) => (num(v) == null ? '—' : `${round1(v)}%`);

export function formatUptime(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '—';
  const s = Math.floor(seconds);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

export function formatBytes(n) {
  if (typeof n !== 'number' || !Number.isFinite(n) || n < 0) return '—';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  let v = n, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  if (i === 0) return `${Math.round(v)} B`;
  return `${v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)} ${units[i]}`;
}

const formatRate = (bps) => (num(bps) == null ? '—' : `${formatBytes(bps)}/s`);

// Inline SVG line; fewer than two finite points is an honest empty state.
export function sparkline(points, { width = 160, height = 32, max } = {}) {
  const w = num(width) > 0 ? width : 160, h = num(height) > 0 ? height : 32;
  const vals = list(points).filter((v) => num(v) != null);
  if (vals.length < 2) return `<span class="hv-spark hv-spark--empty" style="height:${h}px">No history</span>`;
  const hiData = Math.max(...vals), loData = Math.min(...vals);
  const fixed = num(max) != null && max > 0;
  const lo = fixed ? 0 : loData;
  const hi = fixed ? Math.max(max, hiData) : hiData;
  const span = hi - lo || 1;
  const pad = 2, step = (w - pad * 2) / (vals.length - 1);
  const xy = vals.map((v, i) => {
    const x = pad + i * step;
    const y = h - pad - ((v - lo) / span) * (h - pad * 2);
    return `${round1(x)} ${round1(y)}`;
  });
  const d = `M${xy.join(' L')}`;
  const area = `${d} L${round1(pad + (vals.length - 1) * step)} ${h - pad} L${pad} ${h - pad} Z`;
  return `<svg class="hv-spark" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" preserveAspectRatio="none" role="img" aria-label="${vals.length} points, latest ${round1(vals[vals.length - 1])}">`
    + `<path d="${area}" fill="var(--accent-weak, #e8f0fe)" stroke="none"/>`
    + `<path d="${d}" fill="none" stroke="var(--accent, #2563eb)" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>`;
}

export function historyPoints(history) {
  const raw = Array.isArray(history) ? history : Array.isArray(obj(history)?.samples) ? history.samples : [];
  return raw.filter((p) => obj(p));
}
const series = (points, key) => points.map((p) => num(p[key])).filter((v) => v != null);

function healthStage(host, latest, problems, now) {
  if (!latest) return 'unknown';
  const times = [host.last_seen, latest.at].map(reportTimestamp).filter(Number.isFinite);
  const newest = times.length ? new Date(Math.max(...times)).toISOString() : null;
  if (reportFreshness(newest, now) !== 'current') return 'stalled';
  if (problems.some((p) => p.severity === 'critical')) return 'critical';
  return String(host.status || 'unknown');
}

function drifted(versions, pins) {
  const out = new Map();
  const ve = obj(versions) || {}, pe = obj(pins) || {};
  for (const k of VERSION_KEYS) out.set(k, PINNED.includes(k) && k in pe && ve[k] !== pe[k]);
  const vx = obj(ve.pi_extensions) || {}, px = obj(pe.pi_extensions) || {};
  for (const n of new Set([...Object.keys(vx), ...Object.keys(px)])) out.set(`pi/${n}`, n in px && vx[n] !== px[n]);
  return out;
}

const STYLE = `<style>
.hv-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:6px 0 4px}.hv-head h1{margin:0;overflow-wrap:anywhere}
.hv-meta{display:flex;flex-wrap:wrap;gap:6px 14px;color:var(--text-secondary);font-size:.8125rem;margin-bottom:12px}.hv-meta code{overflow-wrap:anywhere}
.hv-badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:.75rem;border:1px solid var(--border-subtle);background:var(--surface-2)}
.hv-badge--warn{color:var(--status-attention,#9a6700)}.hv-badge--crit{color:var(--status-failed,#b42318)}
.hv-problems{list-style:none;margin:0 0 16px;padding:0;display:grid;gap:6px}.hv-problem{display:flex;gap:10px;align-items:baseline;padding:8px 12px;border-radius:var(--radius-sm);border:1px solid var(--border-subtle);border-left:4px solid var(--status-attention,#9a6700);background:var(--surface);overflow-wrap:anywhere}
.hv-problem[data-severity=critical]{border-left-color:var(--status-failed,#b42318)}.hv-problem strong{font-size:.75rem;text-transform:uppercase;letter-spacing:.04em;flex:0 0 auto}
.hv-problem[data-severity=critical] strong{color:var(--status-failed,#b42318)}.hv-problem[data-severity=warning] strong{color:var(--status-attention,#9a6700)}
.hv-range{display:flex;gap:6px;align-items:center;margin:4px 0 10px;font-size:.8125rem}.hv-range a{padding:3px 10px;border-radius:999px;border:1px solid var(--border-subtle);color:var(--text-secondary);text-decoration:none}.hv-range a[aria-current=true]{background:var(--accent-weak);color:var(--accent);font-weight:600}
.hv-value{font-size:1.4rem;font-weight:600}.hv-small{color:var(--text-muted);font-size:.75rem;margin-top:2px;overflow-wrap:anywhere}
.hv-spark{display:block;width:100%;height:auto;margin-top:8px}.hv-spark--empty{display:flex;align-items:center;justify-content:center;border:1px dashed var(--border);border-radius:var(--radius-sm);color:var(--text-muted);font-size:.75rem;margin-top:8px}
.hv-section{margin-top:20px}.hv-section h2{font-size:1rem;margin:0 0 8px}
.hv-table{width:100%;border-collapse:collapse;font-size:.8125rem;table-layout:fixed}.hv-table th,.hv-table td{text-align:left;padding:4px 6px;border-top:1px solid var(--border-subtle);overflow-wrap:anywhere}.hv-table th{color:var(--text-muted);font-weight:500;border-top:0}.hv-table td.hv-num,.hv-table th.hv-num{text-align:right}
.hv-drift{color:var(--status-attention,#9a6700);font-weight:600}
.hv-stats{display:flex;flex-wrap:wrap;gap:6px 18px;margin:0 0 8px;font-size:.875rem}.hv-stats b{font-weight:600}
@media(max-width:650px){.hv-table{font-size:.75rem}.hv-value{font-size:1.2rem}}
</style>`;

function header(host, latest, stage, range, now) {
  const id = encodeURIComponent(String(host.host_id ?? ''));
  const agents = num(latest?.agents_running);
  const meta = [
    latest && num(latest.uptime_s) != null ? `<span>Up ${esc(formatUptime(latest.uptime_s))}</span>` : '',
    agents != null ? `<span>${esc(agents)} agent${agents === 1 ? '' : 's'} running</span>` : '',
    `<span>Last report ${latest ? esc(formatTimestamp(latest.at ?? host.last_seen)) : 'never'}</span>`,
    `<span>Tailnet IP ${host.tailnet_ip ? `<code>${esc(host.tailnet_ip)}</code>` : 'not reported'}</span>`,
    latest && (num(latest.net_rx_bps) != null || num(latest.net_tx_bps) != null) ? `<span>Network in ${esc(formatRate(latest.net_rx_bps))} · out ${esc(formatRate(latest.net_tx_bps))}</span>` : '',
    `<herdr-ident kind="host" id="${show(host.host_id, '')}"></herdr-ident>`,
  ].filter(Boolean).join('');
  const picker = RANGES.map((r) => `<a href="#/hosts/${esc(id)}?range=${r}"${r === range ? ' aria-current="true"' : ''}>${r}</a>`).join('');
  return `<div class="hv-head"><h1>${show(host.host_name ?? host.host_id, 'Unnamed host')}</h1>
    <herdr-status stage="${esc(stage)}"></herdr-status>
    ${latest ? '' : '<span class="hv-badge hv-badge--crit">No telemetry reported</span>'}
    ${host.maintenance === true ? '<span class="hv-badge hv-badge--warn">Maintenance</span>' : ''}</div>
    <div class="hv-meta">${meta}</div>
    <nav class="hv-range" aria-label="History range">History ${picker}</nav>`;
}

function problemsStrip(problems) {
  if (!problems.length) return '';
  return `<ul class="hv-problems" data-section="problems" aria-label="Problems">${problems.map((p) => {
    const sev = p.severity === 'critical' ? 'critical' : 'warning';
    return `<li class="hv-problem" data-severity="${sev}" data-code="${show(p.code, '')}"><strong>${sev}</strong><span>${show(p.message, show(p.code, 'Problem'))}</span></li>`;
  }).join('')}</ul>`;
}

const card = (section, title, value, smalls, spark) => `<div class="hd-card" data-section="${section}"><div class="hd-card__title">${title}</div>
  <div class="hv-value">${value}</div>${smalls.filter(Boolean).map((s) => `<div class="hv-small">${s}</div>`).join('')}${spark}</div>`;
const spark = (points, key, max = 100) => `<div data-series="${key}">${sparkline(series(points, key), { width: 240, height: 40, max })}</div>`;

function gauges(latest, points) {
  const load = [latest.load1, latest.load5, latest.load15].map(num);
  const cpu = card('cpu', 'CPU', esc(pct(latest.cpu)), [
    load.some((v) => v != null) ? `Load ${load.map((v) => (v == null ? '—' : esc(round1(v)))).join(' / ')}` : '',
  ], spark(points, 'cpu'));
  const mem = card('memory', 'Memory', esc(pct(latest.mem)), [
    num(latest.mem_used_b) != null || num(latest.mem_total_b) != null ? `${esc(formatBytes(latest.mem_used_b))} of ${esc(formatBytes(latest.mem_total_b))}` : '',
    num(latest.swap) != null ? `Swap ${esc(pct(latest.swap))}` : '',
  ], spark(points, 'mem'));
  const gpus = list(latest.gpus).filter((g) => obj(g));
  const gpuCards = gpus.map((g, i) => card('gpu', `GPU ${i}${g.name ? ` · ${esc(g.name)}` : ''}`, esc(pct(g.util_pct)), [
    num(g.vram_used_b) != null || num(g.vram_total_b) != null ? `VRAM ${esc(formatBytes(g.vram_used_b))} of ${esc(formatBytes(g.vram_total_b))}` : '',
    num(g.temp_c) != null ? `${esc(round1(g.temp_c))} °C` : '',
    gpus.length > 1 ? 'History: max across GPUs' : '',
  ], spark(points, 'gpu_util_pct'))).join('');
  const mounts = list(latest.mounts).filter((m) => obj(m));
  const primary = num(latest.disk) ?? num(mounts.find((m) => m.path === '/')?.pct) ?? null;
  const mountTable = mounts.length ? `<table class="hv-table" data-mounts style="margin-top:8px"><thead><tr><th>Mount</th><th class="hv-num">Used</th><th class="hv-num">Size</th><th class="hv-num">%</th></tr></thead><tbody>${mounts.map((m) => `<tr><td><code>${show(m.path)}</code></td><td class="hv-num">${esc(formatBytes(m.used_b))}</td><td class="hv-num">${esc(formatBytes(m.total_b))}</td><td class="hv-num">${esc(pct(m.pct))}</td></tr>`).join('')}</tbody></table>` : '';
  const disk = card('disk', 'Disk', esc(pct(primary)), [], spark(points, 'disk') + mountTable);
  return `<div class="hd-grid" data-section="gauges">${cpu}${mem}${gpuCards}${disk}</div>`;
}

function topTable(kind, title, rows) {
  const body = rows.length ? rows.map((r) => `<tr><td><code>${show(r.command)}</code></td><td>${show(r.user)}</td><td class="hv-num">${esc(num(r.cpu_pct) == null ? '—' : round1(r.cpu_pct))}</td><td class="hv-num">${esc(num(r.mem_pct) == null ? '—' : round1(r.mem_pct))}</td><td>${show(r.owner)}</td></tr>`).join('')
    : '<tr><td colspan="5" class="hd-card__sub">No processes reported</td></tr>';
  return `<div class="hd-card"><div class="hd-card__title">${title}</div><table class="hv-table" data-top="${kind}"><thead><tr><th style="width:34%">Command</th><th>User</th><th class="hv-num">CPU %</th><th class="hv-num">Mem %</th><th>Owner</th></tr></thead><tbody>${body}</tbody></table></div>`;
}

function topConsumers(latest) {
  const top = obj(latest.top) || {};
  const pick = (k) => list(top[k]).filter((r) => obj(r));
  return `<section class="hv-section" data-section="top"><h2>Top consumers</h2><div class="hd-grid">${topTable('cpu', 'By CPU', pick('cpu'))}${topTable('mem', 'By memory', pick('mem'))}</div></section>`;
}

function updatesSection(host) {
  const u = obj(host.updates);
  if (!u) return `<section class="hv-section" data-section="updates"><h2>Updates</h2><div class="hd-card hd-card__sub">Updates not yet reported</div></section>`;
  const managerKnown = u.manager != null && u.manager !== '';
  const count = (v) => (managerKnown && num(v) != null ? esc(v) : 'unknown');
  const reboot = u.reboot_required === true ? '<span class="hv-badge hv-badge--warn">Reboot required</span>' : u.reboot_required === false ? 'No reboot needed' : 'Reboot status unknown';
  const units = list(u.failed_units).filter((x) => x != null);
  const pkgs = list(u.packages).filter((p) => obj(p));
  const outdated = num(u.outdated);
  const shown = outdated != null && outdated > pkgs.length ? ` (showing ${pkgs.length} of ${esc(outdated)})` : '';
  const pkgList = pkgs.length ? `<details style="margin-top:8px"><summary>Outdated packages${shown}</summary><table class="hv-table"><thead><tr><th>Package</th><th>Installed</th><th>Available</th></tr></thead><tbody>${pkgs.map((p) => `<tr><td><code>${show(p.name)}</code></td><td>${show(p.from)}</td><td>${show(p.to)}</td></tr>`).join('')}</tbody></table></details>` : '';
  return `<section class="hv-section" data-section="updates"><h2>Updates</h2><div class="hd-card">
    <div class="hv-stats"><span>Outdated <b data-field="outdated">${count(u.outdated)}</b></span><span>Security <b data-field="security">${count(u.security)}</b></span><span>Manager ${show(u.manager, 'unknown')}</span><span>${reboot}</span></div>
    <div class="hv-small">${units.length ? `Failed units: ${units.map((x) => `<code>${esc(x)}</code>`).join(', ')}` : u.failed_units == null ? 'Failed units: unknown' : 'No failed units'}</div>
    ${pkgList}</div></section>`;
}

function versionsSection(host) {
  const v = obj(host.versions), pins = obj(host.pins) || {};
  const fu = obj(host.fleet_update);
  const last = `<div class="hv-small">Last fleet update: ${fu?.last_updated ? esc(formatTimestamp(fu.last_updated)) : 'never recorded'}</div>`;
  if (!v) return `<section class="hv-section" data-section="versions"><h2>Versions</h2><div class="hd-card"><div class="hd-card__sub">Versions not yet reported</div>${last}</div></section>`;
  const drift = drifted(v, pins);
  const vx = obj(v.pi_extensions) || {}, px = obj(pins.pi_extensions) || {};
  const rows = [...drift.entries()].map(([key, d]) => {
    const ext = key.startsWith('pi/') ? key.slice(3) : null;
    const installed = ext != null ? vx[ext] : v[key];
    const pinned = ext != null ? px[ext] : PINNED.includes(key) ? pins[key] : null;
    return `<tr data-version="${esc(key)}" data-drift="${d}"><td>${esc(key)}</td><td>${show(installed, 'not installed')}</td><td>${show(pinned)}</td><td>${d ? '<span class="hv-drift">differs from pin</span>' : ''}</td></tr>`;
  }).join('');
  return `<section class="hv-section" data-section="versions"><h2>Versions</h2><div class="hd-card"><table class="hv-table"><thead><tr><th>Component</th><th>Installed</th><th>Pinned</th><th></th></tr></thead><tbody>${rows}</tbody></table>${last}</div></section>`;
}

export function renderHostDetail(host, history, { range = '1h', now = Date.now() } = {}) {
  const h = obj(host) || {};
  const latest = obj(h.latest);
  const problems = list(h.problems).filter((p) => obj(p));
  const r = RANGES.includes(range) ? range : '1h';
  const points = historyPoints(history);
  const stage = healthStage(h, latest, problems, now);
  const body = latest
    ? gauges(latest, points) + topConsumers(latest)
    : '<herdr-empty message="No telemetry reported"></herdr-empty>';
  return `${STYLE}${header(h, latest, stage, r, now)}${problemsStrip(problems)}${body}${updatesSection(h)}${versionsSection(h)}`;
}
