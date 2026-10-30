// AI sync: per-machine status of the ai-sync config-sync tool (GET /api/v1/ai-sync, fed read-only by each host).
// Reports counts, relative paths and short error text only; every field is untrusted and goes through esc().
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { pollWhileVisible } from './refresh-bus.js';

export const WARN_MS = 3600e3;       // not synced for over an hour
export const STALE_MS = 24 * 3600e3; // not synced for over a day
const STATE_RANK = { stale: 0, warn: 1, unknown: 2, fresh: 3 };
const STAGE = { fresh: 'ok', warn: 'attention', stale: 'failed', unknown: 'unknown' };
const STATE_TEXT = { fresh: 'in sync recently', warn: 'over 1 h', stale: 'over 24 h', unknown: 'never synced' };

export function ageState(iso, now = Date.now()) {
  const t = Date.parse(iso || '');
  if (Number.isNaN(t)) return 'unknown';
  const age = Math.max(0, now - t);
  return age > STALE_MS ? 'stale' : age > WARN_MS ? 'warn' : 'fresh';
}

const ageText = (iso, now) => {
  const t = Date.parse(iso || '');
  if (Number.isNaN(t)) return '—';
  const m = Math.max(0, Math.round((now - t) / 60000));
  return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
};
const count = (v) => (Number.isInteger(v) && v >= 0 ? v : null);
const strings = (v) => (Array.isArray(v) ? v.filter((x) => typeof x === 'string') : []);

export function aiSyncRows(data, now = Date.now()) {
  const hosts = Array.isArray(data?.hosts) ? data.hosts.filter((h) => h && typeof h === 'object') : [];
  return hosts.map((h) => {
    const state = ageState(h.last_sync_ts, now);
    return { host: String(h.host_id ?? ''), version: typeof h.ai_sync_version === 'string' ? h.ai_sync_version : '', envs: strings(h.envs_enabled),
      state, age: ageText(h.last_sync_ts, now), ahead: count(h.ahead), behind: count(h.behind), drift: count(h.drifted_files_count) ?? 0,
      sample: strings(h.drift_sample).slice(0, 20), skipped: count(h.skipped_links_count) ?? 0,
      error: typeof h.last_error === 'string' ? h.last_error : '', checked: ageText(h.checked_at, now) };
  }).sort((a, b) => STATE_RANK[a.state] - STATE_RANK[b.state] || a.host.localeCompare(b.host));
}

export const AI_SYNC_STYLE = `<style>.ais-table{width:100%;border-collapse:collapse;font-size:.8125rem}.ais-table th,.ais-table td{text-align:left;padding:6px 8px;border-top:1px solid var(--border-subtle);overflow-wrap:anywhere;vertical-align:top}.ais-table th{color:var(--text-muted);font-weight:500;border-top:0}
.ais-table tr[data-state=stale] td:first-child{border-left:4px solid var(--status-failed,#b42318)}.ais-table tr[data-state=warn] td:first-child{border-left:4px solid var(--status-attention,#9a6700)}
.ais-err{color:var(--status-failed,#b42318)}.ais-drift{color:var(--status-attention,#9a6700);font-weight:600}.ais-sample{margin:4px 0 0;padding-left:16px;color:var(--text-muted);font-size:.75rem}</style>`;

export function renderAiSync(data, now = Date.now()) {
  const rows = aiSyncRows(data, now);
  if (!rows.length) return '<herdr-empty message="No hosts have reported ai-sync status yet"></herdr-empty>';
  const body = rows.map((r) => `<tr data-host-id="${esc(r.host)}" data-state="${r.state}">
    <td><strong>${esc(r.host)}</strong></td><td>${r.version ? esc(r.version) : '—'}</td><td>${r.envs.length ? esc(r.envs.join(', ')) : '—'}</td>
    <td><herdr-status stage="${STAGE[r.state]}"></herdr-status> <span title="${esc(STATE_TEXT[r.state])}">${esc(r.age)}</span></td>
    <td>${r.ahead === null ? '—' : `ahead ${r.ahead}`} / ${r.behind === null ? '—' : `behind ${r.behind}`}</td>
    <td><span${r.drift ? ' class="ais-drift"' : ''}>${r.drift}</span>${r.sample.length ? `<ul class="ais-sample">${r.sample.map((p) => `<li>${esc(p)}</li>`).join('')}</ul>` : ''}${r.skipped ? `<div class="ais-sample">${r.skipped} skipped</div>` : ''}</td>
    <td>${r.error ? `<span class="ais-err">${esc(r.error)}</span>` : '—'}</td></tr>`).join('');
  return `<table class="ais-table"><thead><tr><th>Host</th><th>Version</th><th>Envs</th><th>Last sync</th><th>Ahead / behind</th><th>Drifted files</th><th>Last error</th></tr></thead><tbody>${body}</tbody></table>`;
}

export const AI_SYNC_POLL_MS = 30000;

export function mountAiSync(root, client, data) {
  root.innerHTML = `${AI_SYNC_STYLE}<section class="ai-sync-page"><div class="hd-live-head"><h1>AI sync</h1><herdr-freshness interval="30000"></herdr-freshness></div>
    <p>Config sync (ai-sync) on every machine. Warns after 1 hour without a sync, stale after 24 hours. Reports counts and file paths only, never contents.</p><div data-ais-list></div></section>`;
  const list = root.querySelector('[data-ais-list]');
  const paint = () => { list.innerHTML = renderAiSync(data); };
  paint();
  return pollWhileVisible(async () => { try { data = await client.listAiSync(); paint(); } catch { /* keep the last list */ } }, AI_SYNC_POLL_MS);
}
