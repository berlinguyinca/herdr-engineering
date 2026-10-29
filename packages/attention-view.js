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

function row(item, now, { busy = false, urls = null } = {}) {
  const sev = SEVERITIES.includes(item.severity) ? item.severity : 'info';
  const href = safeHref(item.href);
  const title = href ? `<a href="${esc(href)}">${esc(item.title)}</a>` : esc(item.title);
  const links = records(item.actions).map((a) => (safeHref(a.href)
    ? `<a class="at-action" href="${esc(safeHref(a.href))}">${esc(a.label || 'Open')}</a>` : '')).join('');
  const count = Number.isFinite(item.count) && item.count > 1 ? ` <span class="at-count">×${item.count}</span>` : '';
  const key = `data-key="${esc(item.key)}" data-fingerprint="${esc(item.fingerprint)}"`;
  const controls = item.state === 'open'
    ? `<button type="button" class="at-btn" data-ack ${key} ${busy ? 'disabled' : ''}>Acknowledge</button>
       <label class="at-snooze">Snooze <select data-snooze ${key} aria-label="Snooze ${esc(item.title)}" ${busy ? 'disabled' : ''}>
         <option value="">…</option>${SNOOZE_CHOICES.map(([s, label]) => `<option value="${s}">${label}</option>`).join('')}</select></label>`
    : `<button type="button" class="at-btn" data-clear ${key} ${busy ? 'disabled' : ''}>${item.state === 'snoozed' ? 'Wake now' : 'Reopen'}</button>`;
  const until = item.state === 'snoozed' && item.snoozed_until ? ` · until ${esc(new Date(item.snoozed_until).toLocaleString())}` : '';
  return `<li class="at-row at-${sev}" data-attention-row="${esc(item.key)}">
    <div class="at-main"><span class="at-sev">${esc(sev)}</span> <strong>${title}</strong>${count}
      <p>${linkifyFabricText(item.detail, urls)}</p>
      <p class="at-meta">${esc(KINDS[item.kind] || text(item.kind))} · first seen ${age(item, now)}${until}</p></div>
    <div class="at-actions">${links}${controls}</div></li>`;
}

function resolvedRow(item, now) {
  const at = Date.parse(item.resolved_at);
  const when = Number.isFinite(at) ? `${esc(formatDuration(Math.max(0, now - at)))} ago` : 'time not reported';
  return `<li class="at-row at-resolved"><div class="at-main"><strong>${esc(item.title)}</strong>
    <p class="at-meta">${esc(KINDS[item.kind] || text(item.kind))} · resolved ${when}</p></div></li>`;
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
      const head = items.slice(0, SHOWN_PER_GROUP).map(renderRow).join('');
      const rest = items.slice(SHOWN_PER_GROUP);
      const more = rest.length
        ? `<li class="at-more"><details><summary>Show all ${items.length} (${rest.length} more)</summary><ul class="at-list">${rest.map(renderRow).join('')}</ul></details></li>` : '';
      return `<section class="at-kind at-kind-${sev}" aria-label="${label}"><h2 class="at-kind-title">${label} <span class="at-count">${items.length}</span>${items.length > 1 ? ` <button type="button" class="at-btn at-ack-all" data-ack-all="${esc(JSON.stringify(items.map((i) => [i.key, i.fingerprint])))}">Acknowledge all ${items.length}</button>` : ''}</h2><ul class="at-list">${head}${more}</ul></section>`;
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
    off: `Browser notifications are off. <button type="button" class="at-btn" data-notify-toggle="on">Enable notifications</button>`,
    on: `Browser notifications are on for ${what}${quiet}. <button type="button" class="at-btn" data-notify-toggle="off">Turn off</button>`,
  }[notify.status];
  return line ? `<p class="at-status" data-notify-status="${esc(notify.status)}">${line}</p>` : '';
}

const STYLES = `<style>
.at-summary{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;margin:8px 0 16px;font-size:14px}
.at-partial{background:var(--surface);border:1px solid var(--status-attention);border-radius:var(--radius);padding:8px 12px;margin:0 0 12px;font-size:13px}
.at-filters{display:flex;flex-wrap:wrap;gap:12px;margin:0 0 12px;font-size:13px}.at-filters select{font:inherit;min-width:0}
.at-list{list-style:none;margin:0;padding:0;display:grid;gap:4px}
.at-row{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:4px 16px;padding:8px 12px;background:var(--surface);border:1px solid var(--border);border-left-width:3px;border-radius:var(--radius)}
.at-critical{border-left-color:var(--status-failed)}.at-warning{border-left-color:var(--status-attention)}.at-info,.at-resolved{border-left-color:var(--border)}
.at-main{min-width:0;flex:1 1 280px}.at-main p{margin:2px 0 0;font-size:12px;overflow-wrap:anywhere}.at-meta{color:var(--text-secondary)}
.at-sev{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--text-secondary)}.at-count{color:var(--text-secondary);font-size:12px}
.at-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:13px}.at-snooze{display:inline-flex;gap:6px;align-items:center}.at-snooze select{font:inherit}
.at-btn{font:inherit;padding:6px 10px;cursor:pointer;min-height:32px}
.at-kind{margin:18px 0}.at-kind-title{font-size:.8125rem;text-transform:uppercase;letter-spacing:.05em;color:var(--text-secondary);margin:0 0 8px;font-weight:600}
.at-ack-all{margin-left:10px;font-size:.75rem;text-transform:none;letter-spacing:0;padding:2px 8px}
.at-more>details>summary{cursor:pointer;padding:8px 12px;color:var(--accent);font-size:.875rem}.at-more>details[open]>summary{margin-bottom:8px}
.at-group{margin:16px 0}.at-group>summary{cursor:pointer;font-weight:600;padding:6px 0}
.at-status{font-size:13px;color:var(--text-secondary);margin:8px 0}
.at-group[data-state=acknowledged] .at-row{opacity:.78}
@media (max-width:600px){.at-actions{width:100%}.at-btn,.at-snooze select{min-height:44px}}
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
  const filterRow = `<div class="at-filters"><label>Severity <select data-filter="severity" aria-label="Severity"><option value="">All</option>${SEVERITIES.map((s) => `<option value="${s}" ${filters.severity === s ? 'selected' : ''}>${s}</option>`).join('')}</select></label>
    <label>Kind <select data-filter="kind" aria-label="Kind"><option value="">All</option>${kinds.map((k) => `<option value="${esc(k)}" ${filters.kind === k ? 'selected' : ''}>${esc(KINDS[k] || k)}</option>`).join('')}</select></label></div>`;
  const summary = `<div class="at-summary" role="status"><span><strong>${Number(counts.open) || 0}</strong> open</span><span>${Number(counts.acknowledged) || 0} acknowledged</span><span>${Number(counts.snoozed) || 0} snoozed</span>${partial ? `<span class="at-partial">Partial: ${esc(unavailable.join(', ') || 'a source')} unavailable</span>` : ''}</div>`;
  const stale = error ? '<p class="at-partial" role="status">Could not refresh; showing the last received list.</p>' : '';
  const trunc = body.truncated ? '<p class="at-status">Showing the first items only; the inbox is capped.</p>' : '';
  let list;
  const renderRow = (i) => row(i, now, { busy: busy.has(i.key), urls: model.urls });
  if (open.length > GROUP_AFTER) list = groupedOpen(open, renderRow);
  else if (open.length) list = `<ul class="at-list" aria-label="Open attention items">${open.map(renderRow).join('')}</ul>`;
  else if (partial) list = '<p class="at-status">Nothing is known to be open, but some sources could not be read.</p>';
  else list = '<p class="at-status">No attention items. Every source was read.</p>';
  const resolved = records(body.resolved);
  return `${STYLES}${head}${summary}${message ? `<p class="at-status" role="status">${esc(message)}</p>` : ''}${notifyLine(model.notify)}${stale}${filterRow}${list}${trunc}
    <div data-state="acknowledged">${group('Acknowledged', acknowledged, (i) => row(i, now, { busy: busy.has(i.key), urls: model.urls }))}</div>
    ${group('Snoozed', snoozed, (i) => row(i, now, { busy: busy.has(i.key), urls: model.urls }))}
    ${group('Recently resolved', resolved, (i) => resolvedRow(i, now))}`;
}

/** Mount the page: polls every POLL_MS while visible; ack/snooze/clear go to the server and re-poll at once. */
export function mountAttention(container, client, { onBody, notifier, onFreshness = () => {} } = {}) {
  const model = { body: null, error: '', loading: true, busy: new Set(), filters: {}, message: '' };
  let disposed = false, timer = null, controller = null, last = '';
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
    const keep = focused?.dataset ? { key: focused.dataset.key, kind: ['ack', 'clear', 'snooze'].find((k) => k in focused.dataset), filter: focused.dataset.filter } : null;
    const openGroups = [...container.querySelectorAll('details[open] > summary')].map((s) => s.textContent.replace(/\s*\(\d+\)$/, ''));
    container.innerHTML = html;
    container.querySelectorAll('details > summary').forEach((s) => {
      if (openGroups.includes(s.textContent.replace(/\s*\(\d+\)$/, ''))) s.parentElement.open = true;
    });
    if (keep) {
      const selector = keep.filter ? `[data-filter="${keep.filter}"]`
        : keep.kind ? `[data-${keep.kind}][data-key="${CSS.escape(keep.key || '')}"]` : null;
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
    model.busy.add(key); model.message = ''; draw();
    try {
      const result = await client.attentionAct(action, body);
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
      if (!pairs.length || !window.confirm(`Acknowledge ${pairs.length} items? They stay listed under Acknowledged.`)) return;
      model.message = ''; pairs.forEach(([k]) => model.busy.add(k)); draw();
      let failed = 0;
      for (const [key, fingerprint] of pairs) {
        try { await client.attentionAct('ack', { key, fingerprint }); } catch { failed += 1; }
        model.busy.delete(key);
      }
      model.message = failed ? `${failed} of ${pairs.length} could not be acknowledged.` : '';
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
    dispose() { disposed = true; clearInterval(clock); clearTimeout(timer); controller?.abort(); document.removeEventListener('visibilitychange', visibility); },
  };
}
