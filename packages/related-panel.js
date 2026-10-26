// "Related" panel for detail pages (cross-linking design 2026-10-24). A plain list of anchors built
// only through entity-links.js; labels are untrusted strings and are escaped. Groups with nothing
// are omitted; a failed or partial answer says so instead of looking like "none".
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { entityHref, forgePrUrl } from './entity-links.js';

const GROUPS = [
  ['mission', 'Mission', 'mission'], ['agents', 'Agents', 'session'], ['host', 'Host', 'host'], ['service', 'Services', 'service'],
  ['ci', 'CI runs', 'ci'], ['review', 'Reviews', 'review'], ['pr', 'Pull requests', 'pr'],
];
const SOURCE_NAME = { sessions: 'agents', missions: 'missions', ci: 'CI', services: 'services', llm: 'LLM usage' };

export const relatedStyles = `<style>.rel{margin:14px 0}.rel h2{font-size:15px;margin:0 0 8px}.rel dl{display:grid;grid-template-columns:minmax(80px,auto) minmax(0,1fr);gap:6px 14px;margin:0}
.rel dt{color:var(--text-secondary)}.rel dd{margin:0;display:flex;flex-wrap:wrap;gap:4px 12px;overflow-wrap:anywhere}.rel .rel-state{color:var(--text-secondary);font-size:12px}
.rel .rel-note{color:var(--text-secondary);font-size:13px;margin:8px 0 0}.rel .hd-inline{margin-top:10px}@media(max-width:600px){.rel dl{display:block}.rel dd{margin:2px 0 10px}}</style>`;

function link(group, kind, item) {
  const label = esc(item.label || item.id);
  const state = item.state ? ` <span class="rel-state">${esc(String(item.state).replaceAll('_', ' '))}</span>` : '';
  const relation = item.relation ? ` <span class="rel-state">${esc(item.relation)}</span>` : '';
  let href = null;
  if (kind === 'ci') href = entityHref('ci', item.run_number ?? item.id, { repository: item.repo_filter });
  else if (kind === 'pr') {
    const url = forgePrUrl(item.repository, item.number);
    return url ? `<span><a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${label}</a> <span class="rel-state">external${item.state ? ` · ${esc(item.state)}` : ''}</span></span>` : `<span>${label}${state}</span>`;
  } else href = entityHref(kind, item.id);
  return href ? `<span><a href="${esc(href)}">${label}</a>${state}${relation}</span>` : `<span>${label}${state}</span>`;
}

function llmRow(llm, sessionId) {
  if (!llm) return '';
  const text = `${llm.requests} requests · ${llm.tokens_out} output tokens${llm.problems ? ` · ${llm.problems} problems` : ''} (24 h)`;
  const href = sessionId ? entityHref('llm-session', sessionId) : null;
  return `<dt>LLM usage</dt><dd>${href ? `<a href="${esc(href)}">${esc(text)}</a>` : esc(text)}</dd>`;
}

/** `answer` is the /api/v1/related body, or null while loading; `error` is a failure message. */
export function renderRelated(answer, { error = null, footer = '', sessionId = null } = {}) {
  if (error) return `${relatedStyles}<section class="rel" aria-label="Related items"><h2>Related</h2><p class="rel-note" role="status">Related items could not be loaded.</p>${footer}</section>`;
  if (!answer) return `${relatedStyles}<section class="rel" aria-label="Related items"><h2>Related</h2><p class="rel-note" role="status">Loading related items…</p>${footer}</section>`;
  const rel = answer.relations || {};
  const more = answer.more || {};
  const itemsFor = (key) => (Array.isArray(rel[key]) ? rel[key] : key === 'service' && Array.isArray(rel.services) ? rel.services : []);
  const moreFor = (key) => more[key] ?? (key === 'service' ? more.services : null);
  const rows = GROUPS.filter(([key]) => itemsFor(key).length).map(([key, title, kind]) => {
    const extra = moreFor(key) ? `<span class="rel-state">+${esc(moreFor(key))} more</span>` : '';
    return `<dt>${esc(title)}</dt><dd>${itemsFor(key).map((item) => link(key, kind, item)).join('')}${extra}</dd>`;
  });
  const llm = llmRow(rel.llm, sessionId);
  const partial = Array.isArray(answer.partial) && answer.partial.length
    ? `<p class="rel-note" role="status">Could not read ${answer.partial.map((s) => esc(SOURCE_NAME[s] || s)).join(', ')}; related items from there may be missing.</p>` : '';
  const body = rows.length || llm ? `<dl>${rows.join('')}${llm}</dl>` : '<p class="rel-note">Nothing else is linked to this yet.</p>';
  return `${relatedStyles}<section class="rel" aria-label="Related items"><h2>Related</h2>${body}${partial}${footer}</section>`;
}

/** Fetch and render into `el`; returns a disposer. A stale answer never overwrites a newer render. */
export function mountRelated(el, client, kind, id, opts = {}) {
  let live = true;
  const controller = new AbortController();
  el.innerHTML = renderRelated(null, opts);
  client.related(kind, id, { signal: controller.signal }).then(
    (answer) => { if (live && el.isConnected) el.innerHTML = renderRelated(answer, opts); },
    (error) => {
      if (!live || !el.isConnected || error?.name === 'AbortError') return;
      // An entity with no stored record (a live-only Pi mission) simply has no recorded neighbours.
      el.innerHTML = error?.status === 404 ? renderRelated({ relations: {} }, opts) : renderRelated(null, { ...opts, error: error?.message || 'failed' });
    });
  return () => { live = false; controller.abort(); };
}
