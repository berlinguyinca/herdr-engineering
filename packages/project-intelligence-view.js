// Project intelligence: project picker, source-linked timeline and an evidence pane (spec 04-UX-UI-MOBILE).
// A pure render of GET /api/v1/intelligence/... : every string is untrusted and escaped; links come only from
// entity-links.js, never from text. Filters live in the URL hash so a view can be shared and reloaded.
// Counts are the server's post-filter numbers; nothing here derives a total from hidden data.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { entityHref } from './entity-links.js';

const arr = (v) => (Array.isArray(v) ? v : []);
const LABEL = { verified: ['Evidence-backed', 'ok'], unverified: ['Unverified', 'warn'] };

export const intelligenceStyles = `<style>.pi{margin:14px 0}.pi-bar{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:end;margin:8px 0 14px}
.pi-bar label{display:flex;flex-direction:column;font-size:12px;color:var(--text-secondary);gap:2px}.pi-bar select,.pi-bar input{font:inherit;padding:4px 8px;border:1px solid var(--border);border-radius:var(--radius);background:transparent;color:inherit;min-width:0}
.pi-split{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:16px}.pi-list{list-style:none;margin:0;padding:0}
.pi-item{display:block;width:100%;text-align:left;padding:8px 10px;border:0;border-bottom:1px solid var(--border);background:transparent;color:inherit;font:inherit;cursor:pointer;overflow-wrap:anywhere}
.pi-item:hover,.pi-item:focus-visible,.pi-item[aria-current=true]{background:color-mix(in srgb,var(--accent) 12%,transparent);outline:none}.pi-item:focus-visible{box-shadow:inset 0 0 0 2px var(--accent)}
.pi-meta{color:var(--text-secondary);font-size:12px}.pi-chip{display:inline-block;padding:1px 8px;border-radius:999px;border:1px solid var(--border);font-size:12px;margin-left:6px}
.pi-chip[data-tone=ok]{color:var(--status-ok,#1a7f37)}.pi-chip[data-tone=warn]{color:var(--status-attention,#9a6700)}.pi-chip[data-tone=info]{color:var(--accent)}
.pi-day{font-size:13px;font-weight:600;margin:14px 0 4px;color:var(--text-secondary)}.pi-pane{border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;min-width:0;overflow-wrap:anywhere}
.pi-pane h2{font-size:15px;margin:0 0 8px}.pi-note{color:var(--text-secondary);font-size:13px}.pi-state{padding:18px 0;color:var(--text-secondary)}
.pi-sup{text-decoration:line-through;opacity:.7}@media(max-width:720px){.pi-split{grid-template-columns:minmax(0,1fr)}.pi-pane{order:-1}}</style>`;

const day = (iso) => String(iso || '').slice(0, 10) || 'unknown date';
const when = (iso) => String(iso || '').replace('T', ' ').replace(/(\.\d+)?(\+00:00|Z)$/, ' UTC');

export function renderTimeline(data, selected) {
  const items = arr(data?.items);
  if (!items.length) return '<p class="pi-state" role="status">No events match these filters.</p>';
  let last = null;
  const rows = items.map((e) => {
    const head = e.occurred_at && day(e.occurred_at) !== last ? `<li class="pi-day" role="presentation">${esc(last = day(e.occurred_at))}</li>` : '';
    const [text, tone] = e.tombstoned ? ['Removed at source', 'warn'] : (LABEL[e.label] || LABEL.unverified);
    return `${head}<li><button class="pi-item${e.superseded ? ' pi-sup' : ''}" data-event="${esc(e.event_id)}" aria-current="${selected === e.event_id}">
<strong>${esc(e.title)}</strong><span class="pi-chip" data-tone="${tone}">${esc(text)}</span>${e.late_arrival ? '<span class="pi-chip" data-tone="info">late arrival</span>' : ''}${e.superseded ? '<span class="pi-chip" data-tone="warn">superseded</span>' : ''}
<div class="pi-meta">${esc(e.category)} · ${esc(e.actor || 'unknown actor')} · <time datetime="${esc(e.occurred_at)}">${esc(when(e.occurred_at))}</time></div></button></li>`;
  }).join('');
  return `<ol class="pi-list" aria-label="Timeline">${rows}</ol><p class="pi-note">${esc(data.total)} visible event${data.total === 1 ? '' : 's'}${data.total > items.length ? `; showing the latest ${esc(items.length)}` : ''}.</p>`;
}

export function renderDetail(e) {
  if (!e) return '<p class="pi-state">Select an event to see its evidence.</p>';
  const ev = arr(e.evidence_refs);
  const refs = ev.length
    ? `<ul>${ev.map((r) => `<li><code>${esc(r.source_ref)}</code> · ${esc(r.anchor_type)} <code>${esc(r.anchor_value)}</code>${r.git_sha ? ` · <code>${esc(String(r.git_sha).slice(0, 12))}</code>` : ''}</li>`).join('')}</ul>`
    : '<p class="pi-note">No evidence reference. Treat this as unverified.</p>';
  const ents = arr(e.involved_entities).map((x) => {
    const kind = String(x).split(':')[0];
    const href = ['pr', 'ci', 'mission', 'session'].includes(kind) ? entityHref(kind, String(x).slice(kind.length + 1)) : null;
    return href ? `<a href="${esc(href)}">${esc(x)}</a>` : esc(x);
  }).join(', ');
  return `<h2>${esc(e.title)}</h2><dl><dt>Happened</dt><dd><time>${esc(when(e.occurred_at))}</time></dd><dt>Observed by Herdr</dt><dd><time>${esc(when(e.observed_at))}</time></dd>
<dt>Actor</dt><dd>${esc(e.actor || 'unknown')}</dd><dt>Related</dt><dd>${ents || '—'}</dd></dl><h2>Evidence</h2>${refs}<h2>Related</h2><div id="pi-related" aria-live="polite"><p class="pi-note">Loading links…</p></div>`;
}

const PRED = { derived_from: 'derived from', attached_to: 'attached to', mentions: 'mentions', fixes: 'fixes', caused_by: 'caused by',
  blocks: 'blocks', relates_to: 'relates to', supersedes: 'supersedes', discusses: 'discusses' };

export function renderRelated(items, ref) {
  const rels = arr(items);
  if (!rels.length) return '<p class="pi-note">No links to or from this item.</p>';
  return `<ul class="pi-rel">${rels.map((r) => {
    const other = r.from === ref ? r.to : r.from;
    const dir = r.from === ref ? '→' : '←';
    const state = r.review_state === 'confirmed' ? ['Confirmed', 'ok'] : r.review_state === 'rejected' ? ['Rejected', 'warn']
      : r.derivation === 'inferred' ? [`Inferred${typeof r.confidence === 'number' ? ` ${Math.round(r.confidence * 100)}%` : ''} · provisional`, 'warn'] : ['Provisional', 'warn'];
    const n = arr(r.evidence_refs).length;
    return `<li><span aria-hidden="true">${dir}</span> ${esc(PRED[r.predicate] || String(r.predicate).replace(/_/g, ' '))} <code>${esc(other)}</code>
<span class="pi-chip" data-tone="${state[1]}">${esc(state[0])}</span><span class="pi-note"> ${n} evidence ref${n === 1 ? '' : 's'}</span></li>`;
  }).join('')}</ul>`;
}

export function renderFindings(items) {
  const f = arr(items);
  if (!f.length) return '<p class="pi-note">No findings are visible to you.</p>';
  return `<ul class="pi-rel">${f.map((x) => `<li>${esc(x.statement)} <span class="pi-chip" data-tone="${x.verified ? 'ok' : 'warn'}">${x.verified ? 'Evidence-backed' : 'Unverified'}</span>
<span class="pi-note"> ${arr(x.evidence_refs).length} evidence ref${arr(x.evidence_refs).length === 1 ? '' : 's'}</span></li>`).join('')}</ul>`;
}

export function mountProjectIntelligence(host, client, { query = new URLSearchParams(), setQuery, onFreshness = () => {} } = {}) {
  let disposed = false;
  let timeline = null;
  const state = { project: query.get('project') || '', source: query.get('source') || '', category: query.get('category') || '',
    as_of: query.get('as_of') || '', event: query.get('event') || '' };
  const persist = () => setQuery?.(Object.fromEntries(Object.entries(state).filter(([, v]) => v)));
  host.innerHTML = `${intelligenceStyles}<section class="pi" aria-label="Project intelligence"><div class="pi-bar"></div><div class="pi-state hd-skeleton-group" data-skeleton aria-busy="true" role="status" aria-label="Loading project intelligence">${'<div class="hd-skeleton" style="height:44px"></div>'.repeat(3)}</div></section>`;
  const bar = host.querySelector('.pi-bar');
  const body = host.querySelector('.pi-state');

  async function load() {
    try {
      const { items: projects } = await client.getJson('/api/v1/intelligence/projects');
      if (disposed) return;
      onFreshness(true);
      if (!arr(projects).length) { body.outerHTML = '<p class="pi-state" role="status">No projects are visible to you yet.</p>'; return; }
      if (!projects.some((p) => p.id === state.project)) state.project = projects[0].id;
      bar.innerHTML = `<label>Project<select id="pi-project">${projects.map((p) => `<option value="${esc(p.id)}"${p.id === state.project ? ' selected' : ''}>${esc(p.name)}</option>`).join('')}</select></label>
<label>Source<input id="pi-source" value="${esc(state.source)}" placeholder="e.g. gmail"></label>
<label>Category<input id="pi-category" value="${esc(state.category)}" placeholder="e.g. ci"></label>
<label>As of (UTC)<input id="pi-asof" type="datetime-local" value="${esc(state.as_of)}"></label>`;
      bar.querySelector('#pi-project').onchange = (ev) => { state.project = ev.target.value; state.event = ''; refresh(); };
      bar.querySelector('#pi-source').onchange = (ev) => { state.source = ev.target.value.trim(); refresh(); };
      bar.querySelector('#pi-category').onchange = (ev) => { state.category = ev.target.value.trim(); refresh(); };
      bar.querySelector('#pi-asof').onchange = (ev) => { state.as_of = ev.target.value; refresh(); };
      await refresh();
    } catch (err) {
      if (disposed) return;
      onFreshness(false, `project intelligence: ${err.status || 'network'}`);
      body.outerHTML = `<p class="pi-state" role="alert">Could not load project intelligence (${esc(err.status || 'network')}). Nothing is shown rather than partial data.</p>`;
    }
  }

  let seq = 0;
  async function refresh() {
    persist();
    const mine = ++seq;                       // a newer refresh makes every older response stale
    const q = new URLSearchParams();
    if (state.source) q.set('source', state.source);
    if (state.category) q.set('category', state.category);
    if (state.as_of) q.set('as_of', `${state.as_of}${/T\d\d:\d\d$/.test(state.as_of) ? ':00' : ''}+00:00`);
    let data;
    try {
      data = await client.getJson(`/api/v1/intelligence/projects/${encodeURIComponent(state.project)}/timeline?${q}`);
    } catch (err) {
      if (disposed || mine !== seq) return;
      onFreshness(false, `timeline: ${err.status || 'network'}`);
      host.querySelector('.pi-state, .pi-split')?.replaceWith(Object.assign(document.createElement('p'), {
        className: 'pi-state', role: 'alert', textContent: `Could not load the timeline (${err.status || 'network'}).` }));
      return;
    }
    if (disposed || mine !== seq) return;
    onFreshness(true);
    timeline = data;
    draw(host.querySelector('.pi-state, .pi-split'));   // re-queried after the await: never a detached node
  }

  let findings = null;
  let relSeq = 0;
  async function loadRelated(ref) {
    const mine = ++relSeq;
    let html;
    try {
      const out = await client.getJson(`/api/v1/intelligence/relationships?ref=${encodeURIComponent(ref)}`);
      html = renderRelated(out?.items, ref);
    } catch (err) {
      html = `<p class="pi-note" role="alert">Could not load links (${esc(err.status || 'network')}).</p>`;
    }
    if (disposed || mine !== relSeq) return;       // a newer selection owns the pane
    const slot = host.querySelector('#pi-related');
    if (slot) slot.innerHTML = html;
  }
  async function loadFindings() {
    try {
      const out = await client.getJson('/api/v1/intelligence/findings');
      findings = arr(out?.items);
    } catch (err) {
      if (disposed) return;
      const slot = host.querySelector('#pi-findings');
      if (slot) slot.innerHTML = `<p class="pi-note" role="alert">Could not load findings (${esc(err.status || 'network')}).</p>`;
      return;
    }
    if (disposed) return;
    const slot = host.querySelector('#pi-findings');
    if (slot) slot.innerHTML = renderFindings(findings);
  }

  function draw(el) {
    const sel = arr(timeline?.items).find((i) => i.event_id === state.event);
    const html = `<div class="pi-split"><div>${renderTimeline(timeline, state.event)}</div><aside class="pi-pane" aria-label="Event details" aria-live="polite">${renderDetail(sel)}</aside></div>`;
    if (!el) return;
    el.outerHTML = html;
    if (!host.querySelector('.pi-findings')) {
      host.querySelector('.pi').insertAdjacentHTML('beforeend', `<section class="pi-findings" aria-label="Findings"><h2>Findings</h2><div id="pi-findings">${findings === null ? '<p class="pi-note">Loading findings…</p>' : renderFindings(findings)}</div></section>`);
      if (findings === null) loadFindings();
    }
    if (sel?.involved_entities?.[0]) loadRelated(sel.involved_entities[0]);
    host.querySelectorAll('.pi-item').forEach((b) => { b.onclick = () => { state.event = b.dataset.event; persist(); draw(host.querySelector('.pi-split')); host.querySelector(`.pi-item[data-event="${CSS.escape(state.event)}"]`)?.focus(); }; });
  }

  load();
  return { dispose() { disposed = true; }, refresh: () => refresh() };
}
