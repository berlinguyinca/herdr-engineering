// Fleet attention page (#/attention). The server derives the items and stores the operator's
// acknowledge and snooze, so every browser shows the same inbox (design 2026-10-24).
// Remote text is untrusted: every dynamic string is escaped and every href is an internal hash link.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { formatDuration } from './herdr-web-components/format.js';
import { skeleton } from './motion.js';
import { requestErrorDetail } from './dashboard-load-state.js';
import { createFabricUrlResolver, linkifyFabricText } from './fabric-urls.js';

const SEVERITIES = ['critical', 'warning', 'info'];
const KINDS = {
  mission_attention: 'Mission needs attention', mission_failed: 'Mission failed', agent_failed: 'Agent failed',
  agent_blocked: 'Agent blocked', ci_failed: 'CI failed', review_changes: 'Review changes requested',
  service_down: 'Service down', host_stale: 'Host silent', control_plane: 'Control plane', fleet_drift: 'Fleet drift',
  llm_problems: 'LLM problems', ci_fix: 'CI fix', deployment_escalation: 'Deployment failed',
  source_unavailable: 'Source unavailable',
};
export const SNOOZE_CHOICES = [[3600, '1 hour'], [14400, '4 hours'], [86400, '24 hours'], [604800, '7 days']];
export const POLL_MS = 5000;

const records = (value) => (Array.isArray(value) ? value.filter((v) => v && typeof v === 'object' && !Array.isArray(v)) : []);
const text = (value) => (typeof value === 'string' ? value : '');
const safeHref = (value) => (typeof value === 'string' && /^#\/[^\s]*$/.test(value) ? value : '');

/** What the nav badge needs from one inbox answer; null when the answer is unusable. */
export function attentionBadge(body) {
  const counts = body && typeof body === 'object' ? body.counts : null;
  if (!counts || !Number.isFinite(counts.open)) return null;
  return { count: counts.open, critical: Number.isFinite(counts.critical) ? counts.critical : 0 };
}

function age(item, now) {
  const at = Date.parse(item.first_seen);
  return Number.isFinite(at)
    ? `<span data-attention-stamp="${esc(item.first_seen)}">${esc(formatDuration(Math.max(0, now - at)))}</span> ago` : 'time not reported';
}

const ICON_CHECK = '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false"><path d="M3 8.5l3.2 3L13 4.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const ICON_BELL_OFF = '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false"><path d="M4 11V7a4 4 0 016-3.5M12 7v4l1 1H3M6.5 14h3M2 2l12 12" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';

const ICON_WARN = '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false"><path d="M8 2l6.5 11.5h-13zM8 6.5v3.2M8 11.6v.1" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';

const when = (value) => { const at = Date.parse(value); return Number.isFinite(at) ? new Date(at).toLocaleString() : 'not reported'; };
const CHEVRON = '<svg class="at-chev" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" focusable="false"><path d="M6 3.5L10.5 8 6 12.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

// Quick inspection: everything the server knows about the item, without leaving the page.
function inspector(item, kindLabel, urls) {
  const href = safeHref(item.href);
  const seen = [href ? { href, label: 'Open' } : null, ...records(item.actions).filter((a) => safeHref(a.href) && safeHref(a.href) !== href)
    .map((a) => ({ href: safeHref(a.href), label: a.label || 'Open' }))].filter(Boolean);
  const links = seen.map((l) => `<a class="at-action" href="${esc(l.href)}">${esc(l.label)}</a>`).join('');
  const handoffs = records(item.handoffs).length ? `<ul class="at-handoffs" aria-label="Handoffs">${records(item.handoffs).map((h) => {
    const who = esc(text(h.agent) || 'another agent');
    const link = safeHref(h.href) ? `<a href="${esc(safeHref(h.href))}">${who}</a>` : who;
    return `<li data-handoff-direction="${esc(text(h.direction))}">${h.direction === 'out' ? 'Handed off to' : 'Received from'} ${link}${text(h.goal) ? `: ${esc(h.goal)}` : ''} <span class="at-count">(${esc(text(h.state) || 'unknown')})</span></li>`;
  }).join('')}</ul>` : '';
  const field = (name, value) => `<div><dt>${name}</dt><dd>${value}</dd></div>`;
  return `<div class="at-inspect" data-attention-inspector="${esc(item.key)}">
    ${text(item.detail).length > 60 || /[\n]|:\/\//.test(text(item.detail)) ? `<p>${linkifyFabricText(item.detail, urls)}</p>` : ''}
    <dl>${field('Kind', esc(kindLabel))}${field('Source', esc(text(item.source) || 'not reported'))}${field('First seen', esc(when(item.first_seen)))}${field('Last seen', esc(when(item.last_seen)))}${Number.isFinite(item.count) && item.count > 1 ? field('Occurrences', String(item.count)) : ''}${field('State', esc(text(item.state) || 'open'))}</dl>
    ${handoffs}
    ${links ? `<p class="at-links">${links}</p>` : ''}</div>`;
}

function row(item, now, { busy = false, urls = null, showKind = false, expanded = false } = {}) {
  const sev = SEVERITIES.includes(item.severity) ? item.severity : 'info';
  const kindLabel = KINDS[item.kind] || text(item.kind);
  const count = Number.isFinite(item.count) && item.count > 1 ? ` <span class="at-count">×${item.count}</span>` : '';
  const key = `data-key="${esc(item.key)}" data-fingerprint="${esc(item.fingerprint)}"`;
  const controls = item.state === 'open'
    ? `<button type="button" class="hd-btn hd-btn--quiet hd-btn--icon at-btn at-icon" data-ack ${key} aria-label="Acknowledge ${esc(item.title)}" title="Acknowledge: keep it listed, stop counting it" ${busy ? 'disabled' : ''}>${ICON_CHECK}</button>
       <label class="at-snooze" title="Snooze: hide it for a while"><span class="at-sr">Snooze ${esc(item.title)}</span><select data-snooze ${key} ${busy ? 'disabled' : ''}>
         <option value="">Snooze</option>${SNOOZE_CHOICES.map(([s, label]) => `<option value="${s}">${label}</option>`).join('')}</select></label>`
    : `<button type="button" class="hd-btn hd-btn--quiet at-btn" data-clear ${key} ${busy ? 'disabled' : ''}>${item.state === 'snoozed' ? 'Wake now' : 'Reopen'}</button>`;
  const until = item.state === 'snoozed' && item.snoozed_until ? ` · until ${esc(new Date(item.snoozed_until).toLocaleString())}` : '';
  const detail = text(item.detail) ? `<span class="at-detail">${esc(item.detail)}</span>` : '';
  return `<li class="at-row at-${sev}" data-attention-row="${esc(item.key)}" data-expanded="${expanded}">
    <button type="button" class="at-main at-toggle" data-expand="${esc(item.key)}" aria-expanded="${expanded}" title="${expanded ? 'Hide' : 'Show'} details">${CHEVRON}<span class="at-sr">${esc(sev)}</span><strong>${esc(item.title)}</strong>${count}${showKind ? `<span class="at-kindlabel">${esc(kindLabel)}</span>` : ''}${detail}</button>
    <div class="at-actions"><span class="at-meta">first seen ${age(item, now)}${until}</span>${controls}</div>
    ${expanded ? inspector(item, kindLabel, urls) : ''}</li>`;
}

function resolvedRow(item, now) {
  const at = Date.parse(item.resolved_at);
  const ago = Number.isFinite(at) ? `${esc(formatDuration(Math.max(0, now - at)))} ago` : 'time not reported';
  return `<li class="at-row at-resolved"><div class="at-main"><strong>${esc(item.title)}</strong><span class="at-kindlabel">${esc(KINDS[item.kind] || text(item.kind))}</span></div>
    <div class="at-actions"><span class="at-meta">resolved ${ago}</span></div></li>`;
}

function group(label, items, render, open = false) {
  if (!items.length) return '';
  return `<details class="at-group" ${open ? 'open' : ''}><summary>${esc(label)} (${items.length})</summary><ul class="at-list">${items.map(render).join('')}</ul></details>`;
}

// A long inbox is mostly repeats of one kind. Past this size, open items fold into one section per severity and
// kind (worst first); the first few of each stay visible and the rest sit behind "Show all".
const GROUP_AFTER = 8;
const SHOWN_PER_GROUP = 3;
function groupedOpen(open, renderRow) {
  const rank = (s) => { const i = SEVERITIES.indexOf(s); return i < 0 ? SEVERITIES.length : i; };
  const buckets = new Map();
  for (const i of open) {
    const k = `${i.severity}|${i.kind}`;
    if (!buckets.has(k)) buckets.set(k, []);
    buckets.get(k).push(i);
  }
  const sections = [...buckets.entries()].sort((a, b) => rank(a[1][0].severity) - rank(b[1][0].severity) || b[1].length - a[1].length)
    .map(([, items]) => {
      const sev = SEVERITIES.includes(items[0].severity) ? items[0].severity : 'info';
      const label = `${esc(sev)} · ${esc(KINDS[items[0].kind] || text(items[0].kind))}`;
      const label1 = KINDS[items[0].kind] || text(items[0].kind);
      const names = items.slice(0, SHOWN_PER_GROUP).map((i) => text(i.title).replace(/^[^·]*·\s*/, '')).join(', ');
      const body = items.length > SHOWN_PER_GROUP
        ? `<li class="at-fold"><details data-fold="${esc(`${items[0].severity}|${items[0].kind}`)}"><summary><strong>${items.length} × ${esc(label1)}</strong><span class="at-detail">${esc(names)} and ${items.length - SHOWN_PER_GROUP} more</span><span class="at-showall">Show all ${items.length}</span></summary><ul class="at-list">${items.map((i) => renderRow(i)).join('')}</ul></details></li>`
        : items.map((i) => renderRow(i)).join('');
      return `<section class="at-kind at-kind-${sev}" aria-label="${label}"><h2 class="at-kind-title">${label} <span class="at-count">${items.length}</span>${items.length > 1 ? ` <button type="button" class="hd-btn hd-btn--quiet hd-btn--sm at-btn at-ack-all" data-ack-all="${esc(JSON.stringify(items.map((i) => [i.key, i.fingerprint])))}">Acknowledge all ${items.length}</button>` : ''}</h2><ul class="at-list">${body}</ul></section>`;
    });
  return `<div aria-label="Open attention items">${sections.join('')}</div>`;
}

const hour = (h) => `${String(h).padStart(2, '0')}:00`;
/** The browser-notification line: its state, the policy the server's YAML sets, and the one control. */
export function notifyLine(notify) {
  if (!notify || notify.status === 'server-off') return '';
  const policy = notify.policy || {};
  const quiet = policy.quiet_hours ? `, quiet ${hour(policy.quiet_hours.start)}–${hour(policy.quiet_hours.end)} in this browser's time` : '';
  const what = `new ${esc(policy.min_severity || 'critical')}${policy.min_severity && policy.min_severity !== 'critical' ? ' and worse' : ''} items`;
  const line = {
    unsupported: 'Browser notifications are not supported in this browser.',
    blocked: 'Browser notifications are blocked. Allow them in this site\'s settings to hear about new critical items.',
    off: `Browser notifications are off. <button type="button" class="hd-btn hd-btn--quiet at-btn" data-notify-toggle="on">Enable notifications</button>`,
    on: `Browser notifications are on for ${what}${quiet}. <button type="button" class="hd-btn hd-btn--quiet at-btn" data-notify-toggle="off">Turn off</button>`,
  }[notify.status];
  return line ? `<p class="at-status" data-notify-status="${esc(notify.status)}">${line}</p>` : '';
}

const STYLES = `<style>
.at-summary{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;margin:16px 0 24px;font-size:14px}.at-summary select{font:inherit;min-width:0}
.at-partial{background:var(--surface);border:1px solid var(--status-attention);border-radius:var(--radius);padding:4px 12px;font-size:13px}
.at-chip{font:inherit;font-size:13px;font-weight:500;padding:0 12px;min-height:var(--control-h-sm);border:1px solid var(--border);border-radius:var(--control-radius);background:var(--surface);color:var(--text-secondary);cursor:pointer}
.at-chip[aria-pressed=true]{border-color:var(--accent);color:var(--text)}.at-chip-critical{border-left:3px solid var(--status-failed)}.at-chip-warning{border-left:3px solid var(--status-attention)}
.at-bell{display:inline-flex;color:var(--text-muted);cursor:help}.at-warn{color:var(--status-attention)}
.at-fold>details>summary{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 12px;cursor:pointer;padding:12px 16px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);font-size:14px}.at-fold>details[open]>summary{margin-bottom:8px}
.at-showall{color:var(--accent);font-size:13px;margin-left:auto}.at-fold .at-detail{flex:1 1 200px}
.at-list{list-style:none;margin:0;padding:0;display:grid;gap:8px}
.at-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px 24px;padding:8px 16px;background:var(--surface);border:1px solid var(--border);border-left-width:3px;border-radius:var(--radius);transition:background-color .15s ease,border-color .15s ease}.at-row:hover{background:var(--surface-hover)}
.at-critical{border-left-color:var(--status-failed)}.at-warning{border-left-color:var(--status-attention)}.at-resolved{opacity:.7}.at-info,.at-resolved{border-left-color:var(--border)}
.at-main{min-width:0;flex:1 1 auto;display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 12px;font-size:14px}
.at-toggle{font:inherit;color:inherit;text-align:left;background:none;border:0;padding:8px 0;cursor:pointer;position:relative;padding-left:20px}.at-toggle:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:var(--radius)}
.at-chev{position:absolute;left:0;top:12px;color:var(--text-muted);transition:transform .15s ease}.at-row[data-expanded=true] .at-chev{transform:rotate(90deg)}
.at-toggle:hover strong{color:var(--accent)}
.at-inspect{flex:1 1 100%;margin:0 0 8px;padding:12px 0 8px 20px;border-top:1px solid var(--border-subtle);font-size:13px;animation:at-open .15s ease}
.at-inspect p{margin:0 0 12px;overflow-wrap:anywhere}.at-inspect dl{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px 24px;margin:0 0 12px}
.at-inspect dt{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--text-muted)}.at-inspect dd{margin:0;color:var(--text-secondary)}.at-links{display:flex;gap:16px}.at-handoffs{list-style:none;margin:0 0 12px;padding:0;display:grid;gap:4px}
@keyframes at-open{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}@media (prefers-reduced-motion:reduce){.at-inspect{animation:none}.at-row,.at-chev{transition:none}}.at-main strong{font-weight:600}
.at-kindlabel{color:var(--text-muted);font-size:12px}
.at-detail{flex:1 1 100%;min-width:0;color:var(--text-secondary);font-size:13px;line-height:1.4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.at-meta{color:var(--text-muted);font-size:12px;white-space:nowrap;margin-right:4px}.at-count{color:var(--text-secondary);font-size:12px}
.at-sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.at-actions{display:flex;gap:12px;align-items:center;font-size:13px;flex:0 0 auto}.at-snooze{display:inline-flex;align-items:center}.at-snooze select{font:inherit;font-size:13px;min-height:32px;max-width:104px;background:transparent;color:var(--text-secondary);border:1px solid transparent;border-radius:var(--radius);cursor:pointer}.at-snooze select:hover{border-color:var(--border);color:var(--text)}
.at-kind{margin:32px 0}.at-kind-title{font-size:.8125rem;text-transform:uppercase;letter-spacing:.05em;color:var(--text-secondary);margin:0 0 8px;font-weight:600}
.at-ack-all{margin-left:8px;text-transform:none;letter-spacing:0}
.at-more>details>summary{cursor:pointer;padding:4px 12px;color:var(--accent);font-size:.875rem}.at-more>details[open]>summary{margin-bottom:8px}
.at-group{margin:24px 0 16px}.at-group>summary{cursor:pointer;font-weight:600;padding:4px 0}
.at-status{font-size:13px;color:var(--text-secondary);margin:8px 0}
.at-group[data-state=acknowledged] .at-row{opacity:.78}
@media (max-width:900px){.at-row{flex-wrap:wrap}.at-detail{white-space:normal}.at-actions{width:100%;flex-wrap:wrap}.at-snooze select,.at-chip{min-height:44px}}
</style>`;

/**
 * Pure render of one model: {body, error, loading, busy:Set, filters:{kind,severity}, message}.
 * "No items" appears only when the answer is complete; a partial answer says which sources are unknown.
 */
export function renderAttention(model = {}, now = Date.now()) {
  const { body, error, loading, filters = {}, message } = model;
  const busy = model.busy instanceof Set ? model.busy : new Set();
  const head = '<h1 tabindex="-1">Attention</h1>';
  if (!body) {
    if (error) return `${STYLES}${head}<herdr-error message="${esc(error)}"></herdr-error><p class="at-status" role="status">Could not load the attention inbox; retrying automatically.</p>`;
    return `${STYLES}${head}${loading === false ? '' : skeleton('attention', { rows: 3 })}`;
  }
  const items = records(body.items).filter((i) => (!filters.kind || i.kind === filters.kind)
    && (!filters.severity || i.severity === filters.severity));
  const open = items.filter((i) => i.state === 'open');
  const acknowledged = items.filter((i) => i.state === 'acknowledged');
  const snoozed = items.filter((i) => i.state === 'snoozed');
  const counts = body.counts || {};
  const unavailable = Array.isArray(body.unavailable) ? body.unavailable.map(text).filter(Boolean) : [];
  const partial = body.partial === true || unavailable.length > 0;
  const kinds = [...new Set(records(body.items).map((i) => i.kind))].sort();
  const sevCount = (sev) => records(body.items).filter((i) => i.state === 'open' && i.severity === sev).length;
  const chips = SEVERITIES.filter((sev) => sevCount(sev) || filters.severity === sev).map((sev) =>
    `<button type="button" class="at-chip at-chip-${sev}" data-filter="severity" data-value="${sev}" aria-pressed="${filters.severity === sev}" title="Show only ${sev} items">${sevCount(sev)} ${sev}</button>`).join('');
  const kindSelect = kinds.length > 1 ? `<select data-filter="kind" aria-label="Kind" title="Filter by kind"><option value="">All kinds</option>${kinds.map((k) => `<option value="${esc(k)}" ${filters.kind === k ? 'selected' : ''}>${esc(KINDS[k] || k)}</option>`).join('')}</select>` : '';
  const quiet = (n, word) => (Number(n) ? `<span>${Number(n)} ${word}</span>` : '');
  const blocked = model.notify && (model.notify.status === 'blocked' || model.notify.status === 'unsupported')
    ? `<span class="at-bell" tabindex="0" role="img" aria-label="Browser notifications unavailable" title="${model.notify.status === 'blocked' ? "Browser notifications are blocked. Allow them in this site's settings to hear about new critical items." : 'Browser notifications are not supported in this browser.'}" data-notify-status="${esc(model.notify.status)}">${ICON_BELL_OFF}</span>` : '';
  const summary = `<div class="at-summary" role="status"><span><strong>${Number(counts.open) || 0}</strong> open</span>${chips}${quiet(counts.acknowledged, 'acknowledged')}${quiet(counts.snoozed, 'snoozed')}${kindSelect}${blocked}${partial ? `<span class="at-bell at-warn" tabindex="0" role="img" title="Partial: ${esc(unavailable.join(', ') || 'a source')} unavailable" aria-label="Partial: ${esc(unavailable.join(', ') || 'a source')} unavailable">${ICON_WARN}</span>` : ''}</div>`;
  // Hosts the operator listed as expected offline: named quietly, never counted or alerted.
  const off = records(body.expected_off).filter((h) => text(h.name));
  const expectedOff = off.length ? `<p class="at-status at-expected" data-expected-off>Expected offline: ${off.map((h) => {
    const at = Date.parse(h.last_seen);
    return `${esc(text(h.name))}${Number.isFinite(at) ? ` (last seen ${esc(formatDuration(Math.max(0, now - at)))} ago)` : ''}`;
  }).join(', ')}.</p>` : '';
  const stale = error ? '<p class="at-partial" role="status">Could not refresh; showing the last received list.</p>' : '';
  const trunc = body.truncated ? '<p class="at-status">Showing the first items only; the inbox is capped.</p>' : '';
  let list;
  const expandedKeys = model.expanded instanceof Set ? model.expanded : new Set();
  const rowOpts = (i, extra = {}) => ({ busy: busy.has(i.key), urls: model.urls, expanded: expandedKeys.has(i.key), ...extra });
  const renderRow = (i, showKind = false) => row(i, now, rowOpts(i, { showKind }));
  const repeated = Object.values(open.reduce((m, i) => { const k = `${i.severity}|${i.kind}`; m[k] = (m[k] || 0) + 1; return m; }, {})).some((n) => n > SHOWN_PER_GROUP);
  if (open.length > GROUP_AFTER || repeated) list = groupedOpen(open, renderRow);
  else if (open.length) list = `<ul class="at-list" aria-label="Open attention items">${open.map((i) => renderRow(i, true)).join('')}</ul>`;
  else if (partial) list = '<p class="at-status">Nothing is known to be open, but some sources could not be read.</p>';
  else list = '<p class="at-status">No attention items. Every source was read.</p>';
  const resolved = records(body.resolved);
  return `${STYLES}${head}${summary}${expectedOff}${message ? `<p class="at-status" role="status">${esc(message)}${model.undo ? ' <button type="button" class="hd-btn hd-btn--quiet at-btn" data-undo>Undo</button>' : ''}</p>` : ''}${blocked ? '' : notifyLine(model.notify)}${stale}${list}${trunc}
    <div data-state="acknowledged">${group('Acknowledged', acknowledged, (i) => row(i, now, rowOpts(i)))}</div>
    ${group('Snoozed', snoozed, (i) => row(i, now, rowOpts(i)))}
    ${group('Recently resolved', resolved, (i) => resolvedRow(i, now))}`;
}

/** Mount the page: polls every POLL_MS while visible; ack/snooze/clear go to the server and re-poll at once. */
export function mountAttention(container, client, { onBody, notifier, onFreshness = () => {} } = {}) {
  const model = { body: null, error: '', loading: true, busy: new Set(), expanded: new Set(), filters: {}, message: '' };
  let disposed = false, timer = null, controller = null, last = '', undoTimer = null;
  const offerUndo = (message, pairs) => {
    clearTimeout(undoTimer); model.message = message; model.undo = pairs;
    undoTimer = setTimeout(() => { model.message = ''; model.undo = null; last = ''; draw(); }, 10000);
  };
  // agent-written detail text may name a loopback service; link it to its fabric address when the gateway knows it
  const fabric = typeof client.resolveFabricUrls === 'function'
    ? createFabricUrlResolver(client, { onChange: () => { last = ''; draw(); } }) : null;
  model.urls = fabric?.resolver || null;
  function draw() {
    if (disposed) return;
    model.notify = notifier ? { status: notifier.status(), policy: model.body?.notify } : null;
    const html = renderAttention(model, Date.now());
    const signature = html.replace(/(<span data-attention-stamp="[^"]*">)[^<]*(<\/span>)/g, '$1$2');
    if (signature === last) return;
    last = signature;
    const focused = container.contains(document.activeElement) ? document.activeElement : null;
    const keep = focused?.dataset ? { key: focused.dataset.key, kind: ['ack', 'clear', 'snooze', 'expand'].find((k) => k in focused.dataset), filter: focused.dataset.filter, value: focused.dataset.value } : null;
    const openGroups = [...container.querySelectorAll('details[open] > summary')].map((s) => s.textContent.replace(/\s*\(\d+\)$/, ''));
    const openFolds = [...container.querySelectorAll('details[data-fold][open]')].map((d) => d.dataset.fold);
    container.innerHTML = html;
    container.querySelectorAll('details[data-fold]').forEach((d) => { if (openFolds.includes(d.dataset.fold)) d.open = true; });
    container.querySelectorAll('details > summary').forEach((s) => {
      if (openGroups.includes(s.textContent.replace(/\s*\(\d+\)$/, ''))) s.parentElement.open = true;
    });
    if (keep) {
      const selector = keep.filter ? `[data-filter="${keep.filter}"]${keep.value ? `[data-value="${CSS.escape(keep.value)}"]` : ''}`
        : keep.kind === 'expand' ? `[data-expand="${CSS.escape(focused.dataset.expand)}"]` : keep.kind ? `[data-${keep.kind}][data-key="${CSS.escape(keep.key || '')}"]` : null;
      (selector && container.querySelector(selector) || container.querySelector('h1'))?.focus({ preventScroll: true });
    }
  }
  async function poll() {
    if (disposed || document.hidden || controller) return;
    clearTimeout(timer);
    const request = new AbortController(); controller = request;
    try {
      const body = await client.attention({ state: 'all' }, { signal: AbortSignal.any([request.signal, AbortSignal.timeout(15000)]) });
      if (disposed || request.signal.aborted || controller !== request) return;
      if (!body || !Array.isArray(body.items)) throw new Error('Invalid response');
      model.body = body; model.error = ''; model.loading = false;
      const live = new Set(body.items.map((i) => i?.key));
      for (const k of model.expanded) if (!live.has(k)) model.expanded.delete(k);
      fabric?.prefetch(...(Array.isArray(body?.items) ? body.items : []).map((i) => i?.detail));
      onBody?.(body);
      onFreshness(true);
    } catch (err) {
      if (disposed || request.signal.aborted || controller !== request) return;
      model.error = err?.status === 404 ? 'The attention inbox is turned off on this server.' : requestErrorDetail(err);
      model.loading = false;
      onFreshness(err?.status === 404, model.error);
    } finally {
      if (controller === request) {
        controller = null;
        draw();
        if (!disposed && !document.hidden) timer = setTimeout(poll, POLL_MS);
      }
    }
  }
  async function act(action, body, key) {
    clearTimeout(undoTimer); model.busy.add(key); model.message = ''; model.undo = null; draw();
    try {
      const result = await client.attentionAct(action, body);
      if (action === 'ack' && !(result && result.stored === false)) offerUndo('Acknowledged.', [[body.key, body.fingerprint]]);
      if (result && result.stored === false && result.reason === 'not_current') model.message = 'That item changed or cleared before your action; the list was refreshed.';
    } catch (err) {
      model.message = `Could not ${action === 'ack' ? 'acknowledge' : action}: ${err?.message || 'request failed'}`;
    } finally {
      model.busy.delete(key);
      clearTimeout(timer); controller?.abort(); controller = null;
      poll();
    }
  }
  container.addEventListener('click', async (event) => {
    const toggle = event.target.closest?.('[data-notify-toggle]');
    if (toggle && notifier) {
      if (toggle.dataset.notifyToggle === 'on') await notifier.enable(); else notifier.disable();
      last = ''; draw();
      return;
    }
    const all = event.target.closest?.('[data-ack-all]');
    if (all) {
      let pairs = [];
      try { pairs = JSON.parse(all.dataset.ackAll).filter((p) => Array.isArray(p) && typeof p[0] === 'string'); } catch { /* No pairs: nothing to do. */ }
      if (!pairs.length) return;
      model.message = ''; pairs.forEach(([k]) => model.busy.add(k)); draw();
      let failed = 0;
      for (const [key, fingerprint] of pairs) {
        try { await client.attentionAct('ack', { key, fingerprint }); } catch { failed += 1; }
        model.busy.delete(key);
      }
      if (failed) { clearTimeout(undoTimer); model.message = `${failed} of ${pairs.length} could not be acknowledged.`; model.undo = failed === pairs.length ? null : pairs; } else offerUndo(`Acknowledged ${pairs.length} items.`, pairs);
      clearTimeout(timer); controller?.abort(); controller = null; poll();
      return;
    }
    const toggleRow = event.target.closest?.('[data-expand]');
    if (toggleRow) {
      const k = toggleRow.dataset.expand;
      if (!model.expanded.delete(k)) model.expanded.add(k);
      last = ''; draw();
      return;
    }
    const chip = event.target.closest?.('button[data-filter]');
    if (chip) {
      const f = chip.dataset.filter, v = chip.dataset.value;
      model.filters = { ...model.filters, [f]: model.filters[f] === v ? '' : v };
      last = ''; draw();
      return;
    }
    const undo = event.target.closest?.('[data-undo]');
    if (undo && model.undo) {
      const pairs = model.undo; clearTimeout(undoTimer); model.undo = null; model.message = '';
      for (const [key, fingerprint] of pairs) { try { await client.attentionAct('clear', { key, fingerprint }); } catch { /* the next poll shows the truth */ } }
      clearTimeout(timer); controller?.abort(); controller = null; poll();
      return;
    }
    const b = event.target.closest?.('[data-ack],[data-clear]');
    if (!b) return;
    act(b.hasAttribute('data-ack') ? 'ack' : 'clear', { key: b.dataset.key, fingerprint: b.dataset.fingerprint }, b.dataset.key);
  });
  container.addEventListener('change', (event) => {
    const el = event.target;
    if (el.dataset?.snooze !== undefined && el.value) {
      act('snooze', { key: el.dataset.key, fingerprint: el.dataset.fingerprint, seconds: Number(el.value) }, el.dataset.key);
    } else if (el.dataset?.filter) {
      model.filters = { ...model.filters, [el.dataset.filter]: el.value };
      last = ''; draw();
    }
  });
  const visibility = () => { if (document.hidden) { clearTimeout(timer); controller?.abort(); controller = null; } else poll(); };
  document.addEventListener('visibilitychange', visibility);
  draw(); poll();
  const clock = setInterval(() => {
    draw();
    const now = Date.now();
    container.querySelectorAll('[data-attention-stamp]').forEach((el) => {
      const at = Date.parse(el.dataset.attentionStamp);
      if (Number.isFinite(at)) el.textContent = formatDuration(Math.max(0, now - at));
    });
  }, 1000);
  return {
    refresh: poll,
    dispose() { disposed = true; clearTimeout(undoTimer); clearInterval(clock); clearTimeout(timer); controller?.abort(); document.removeEventListener('visibilitychange', visibility); },
  };
}
