// Project intelligence: project picker, source-linked timeline and an evidence pane (spec 04-UX-UI-MOBILE).
// A pure render of GET /api/v1/intelligence/... : every string is untrusted and escaped; links come only from
// entity-links.js, never from text. Filters live in the URL hash so a view can be shared and reloaded.
// Counts are the server's post-filter numbers; nothing here derives a total from hidden data.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { entityHref } from './entity-links.js';

const arr = (v) => (Array.isArray(v) ? v : []);
const LABEL = { verified: ['Evidence-backed', 'ok'], unverified: ['Unverified', 'warn'] };

export const intelligenceStyles = `<style>.pi{--pi-ok:#0b6b3a;--pi-warn:#8a4b00;--pi-info:#1e40af;--pi-muted:#4b4b5c;--pi-focus:#0b4fc4;--pi-link:#0b4fc4;margin:20px auto;max-width:1180px;line-height:1.5}
:root[data-theme=dark] .pi{--pi-ok:#34d399;--pi-warn:#fbbf24;--pi-info:#93c5fd;--pi-muted:#b4bacb;--pi-focus:#8ab4ff;--pi-link:#8ab4ff}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]) .pi{--pi-ok:#34d399;--pi-warn:#fbbf24;--pi-info:#93c5fd;--pi-muted:#b4bacb;--pi-focus:#8ab4ff;--pi-link:#8ab4ff}}
.pi a{color:var(--pi-link)}.pi :focus-visible{outline:2px solid var(--pi-focus);outline-offset:1px}.pi ::placeholder{color:var(--pi-muted);opacity:1}.pi-bar{display:flex;flex-wrap:wrap;gap:12px 20px;align-items:end;margin:12px 0 22px}
.pi-bar label{display:flex;flex-direction:column;font-size:12px;color:var(--pi-muted);gap:4px}.pi-bar select,.pi-bar input{font:inherit;padding:7px 10px;border:1px solid var(--border);border-radius:var(--radius);background:transparent;color:inherit;min-width:0}
.pi-split{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:28px}.pi-list{list-style:none;margin:0;padding:0}
.pi-item{display:block;width:100%;text-align:left;padding:14px 16px;border:0;border-bottom:1px solid var(--border);background:transparent;color:inherit;font:inherit;cursor:pointer;overflow-wrap:anywhere}
.pi-item:hover,.pi-item:focus-visible,.pi-item[aria-current=true]{background:color-mix(in srgb,var(--accent) 7%,transparent)}
.pi-meta{color:var(--pi-muted);font-size:12px}.pi-chip{display:inline-block;padding:1px 8px;border-radius:999px;border:1px solid var(--border);font-size:12px;margin-left:8px}
.pi-chip[data-tone=ok]{color:var(--pi-ok)}.pi-chip[data-tone=warn]{color:var(--pi-warn)}.pi-chip[data-tone=info]{color:var(--pi-info)}
.pi-day{font-size:13px;font-weight:600;margin:22px 0 6px;color:var(--pi-muted)}.pi-pane{border:1px solid var(--border);border-radius:var(--radius);padding:18px 22px;min-width:0;overflow-wrap:anywhere}
.pi-pane h2{font-size:15px;margin:0 0 10px}.pi-pane h2:not(:first-child){margin-top:20px}.pi-meta{margin-top:4px}.pi-findings{margin-top:28px}.pi-findings h2,.pi-approvals h2{font-size:15px}.pi-rel{padding-left:20px}.pi-rel li{margin:6px 0}.pi-note{color:var(--pi-muted);font-size:13px}.pi-state{padding:18px 0;color:var(--pi-muted)}
.pi-cards{list-style:none;margin:0;padding:0;display:grid;gap:10px}.pi-card{border:1px solid var(--border);border-radius:var(--radius);padding:10px 14px;overflow-wrap:anywhere}.pi-card dl{margin:6px 0}.pi-card dt{color:var(--pi-muted);font-size:12px}.pi-card dd{margin:0 0 4px}.pi-actions{display:flex;gap:8px;margin-top:8px}.pi-actions button{font:inherit;padding:6px 14px;border:1px solid var(--border);border-radius:var(--radius);background:transparent;color:inherit;cursor:pointer;min-height:32px}.pi-approvals h2{font-size:15px;margin:14px 0 6px}
.pi-sup{text-decoration:line-through;opacity:.7}@media(max-width:720px){.pi-split{grid-template-columns:minmax(0,1fr)}}</style>`;

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
    ? `<ul>${ev.map((r) => `<li><code>${esc(r.source_ref)}</code> · ${esc(r.anchor_type)} <code>${esc(r.anchor_value)}</code>${r.git_sha ? ` · <code>${esc(String(r.git_sha).slice(0, 12))}</code>` : ''}${r.content_hash ? ` · sha256 <code>${esc(String(r.content_hash).slice(0, 12))}</code>` : ''}</li>`).join('')}</ul>`
    : '<p class="pi-note">No evidence reference. Treat this as unverified.</p>';
  const ents = arr(e.involved_entities).map((x) => {
    const kind = String(x).split(':')[0];
    const href = ['pr', 'ci', 'mission', 'session', 'artifact'].includes(kind) ? entityHref(kind, String(x).slice(kind.length + 1)) : null;
    return href ? `<a href="${esc(href)}">${esc(x)}</a>` : esc(x);
  }).join(', ');
  return `<h2>${esc(e.title)}</h2><dl><dt>Happened</dt><dd><time>${esc(when(e.occurred_at))}</time></dd><dt>Observed by Herdr</dt><dd><time>${esc(when(e.observed_at))}</time></dd>
<dt>Actor</dt><dd>${esc(e.actor || 'unknown')}</dd><dt>About</dt><dd>${ents || '—'}</dd></dl><h2>Evidence</h2>${refs}<h2>Related</h2><div id="pi-related" aria-live="polite"><p class="pi-note">Loading links…</p></div>`;
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

const ACTION = { request_ci_repair: 'Request a CI repair', propose_issue: 'Propose an issue', link_entities: 'Link two items', pause_ingestion: 'Pause ingestion' };

export function renderProposals(items) {
  const list = arr(items);
  if (!list.length) return '<p class="pi-note">Nothing is waiting for approval.</p>';
  return `<ul class="pi-cards">${list.map((p) => `<li class="pi-card"><strong>${esc(ACTION[p.action] || p.action)}</strong>
<dl>${Object.entries(p.payload || {}).slice(0, 8).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(typeof v === 'string' ? v : JSON.stringify(v))}</dd>`).join('')}
<dt>Requested by</dt><dd>${esc(p.requester)}</dd><dt>Expires</dt><dd><time datetime="${esc(p.expires_at)}">${esc(when(p.expires_at))}</time></dd></dl>
<div class="pi-actions"><button type="button" data-act="approve" data-id="${esc(p.id)}" data-sha="${esc(p.payload_sha256)}" data-ver="${esc(p.resource_version)}">Approve</button>
<button type="button" data-act="reject" data-id="${esc(p.id)}">Reject</button></div></li>`).join('')}</ul>
<p class="pi-note">Approving records permission for exactly the payload shown. Nothing runs automatically in this release.</p>`;
}

export function mountProjectIntelligence(host, client, { query = new URLSearchParams(), setQuery, onFreshness = () => {} } = {}) {
  let disposed = false;
  let timeline = null;
  const state = { investigation: query.get('investigation') || '', project: query.get('project') || '', source: query.get('source') || '', category: query.get('category') || '',
    as_of: query.get('as_of') || '', event: query.get('event') || '' };
  const persist = () => setQuery?.(Object.fromEntries(Object.entries(state).filter(([, v]) => v)));
  host.innerHTML = `${intelligenceStyles}<section class="pi" aria-label="Project intelligence"><div class="pi-bar"></div><div class="pi-state hd-skeleton-group" data-skeleton aria-busy="true" role="status" aria-label="Loading project intelligence">${'<div class="hd-skeleton" style="height:44px"></div>'.repeat(3)}</div></section>`;
  const bar = host.querySelector('.pi-bar');
  const body = host.querySelector('.pi-state');

  async function load() {
    try {
      const { items: projects } = await client.getJson('/api/v1/intelligence/projects');
      // Investigations are optional context: their failure never blocks the timeline.
      const investigations = await client.getJson('/api/v1/intelligence/investigations').then((o) => arr(o?.items), () => []);
      if (disposed) return;
      onFreshness(true);
      if (!arr(projects).length) { body.outerHTML = '<p class="pi-state" role="status">No projects are visible to you yet.</p>'; return; }
      if (!projects.some((p) => p.id === state.project)) state.project = projects[0].id;
      bar.innerHTML = `<label>Project<select id="pi-project">${projects.map((p) => `<option value="${esc(p.id)}"${p.id === state.project ? ' selected' : ''}>${esc(p.name)}</option>`).join('')}</select></label>
${arr(investigations).length ? `<label>Investigation<select id="pi-inv"><option value="">All</option>${investigations.map((i) => `<option value="${esc(i.id)}"${i.id === state.investigation ? ' selected' : ''}>${esc(i.name)}</option>`).join('')}</select></label>` : ''}
<label>Source<input id="pi-source" value="${esc(state.source)}" placeholder="e.g. gmail"></label>
<label>Category<input id="pi-category" value="${esc(state.category)}" placeholder="e.g. ci"></label>
<label>As of (UTC)<input id="pi-asof" type="datetime-local" value="${esc(state.as_of)}"></label>`;
      bar.querySelector('#pi-project').onchange = (ev) => { state.project = ev.target.value; state.event = ''; refresh(); };
      const inv = bar.querySelector('#pi-inv');
      if (inv) inv.onchange = (ev) => { state.investigation = ev.target.value; refresh(); };
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
    if (state.investigation) q.set('investigation', state.investigation);
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
  async function loadProposals(message = '') {
    let html;
    try {
      const out = await client.getJson('/api/v1/intelligence/proposals');
      html = renderProposals(out?.items);
    } catch (err) {
      html = `<p class="pi-note" role="alert">Could not load approvals (${esc(err.status || 'network')}).</p>`;
    }
    if (disposed) return;
    const slot = host.querySelector('#pi-proposals');
    if (!slot) return;
    slot.innerHTML = `${message ? `<p class="pi-note" role="status">${esc(message)}</p>` : ''}${html}`;
    slot.querySelectorAll('button[data-act]').forEach((b) => {
      b.onclick = async () => {
        b.disabled = true;
        const body = b.dataset.act === 'approve' ? { payload_sha256: b.dataset.sha, resource_version: Number(b.dataset.ver) } : {};
        try {
          await client.request(`/api/v1/intelligence/proposals/${encodeURIComponent(b.dataset.id)}/${b.dataset.act}`, { method: 'POST', body });
          loadProposals(b.dataset.act === 'approve' ? 'Approved.' : 'Rejected.');
        } catch (err) {
          loadProposals(`Not done: ${err.status === 409 ? (err.message || 'conflict') : `error ${err.status || 'network'}`}`);
        }
      };
    });
  }
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
    if (!host.querySelector('.pi-approvals')) {
      host.querySelector('.pi-bar').insertAdjacentHTML('afterend', '<section class="pi-approvals" aria-label="Awaiting approval"><h2>Awaiting approval</h2><div id="pi-proposals" aria-live="polite"><p class="pi-note">Loading approvals…</p></div></section>');
      loadProposals();
    }
    if (!host.querySelector('.pi-findings')) {
      host.querySelector('.pi').insertAdjacentHTML('beforeend', `<section class="pi-findings" aria-label="Findings"><h2>Findings</h2><div id="pi-findings">${findings === null ? '<p class="pi-note">Loading findings…</p>' : renderFindings(findings)}</div></section>`);
      if (findings === null) loadFindings();
    }
    if (sel?.involved_entities?.[0]) loadRelated(sel.involved_entities[0]);
    host.querySelectorAll('.pi-item').forEach((b) => { b.onclick = () => { state.event = b.dataset.event; persist(); draw(host.querySelector('.pi-split')); host.querySelector(`.pi-item[data-event="${CSS.escape(state.event)}"]`)?.focus(); if (window.matchMedia?.('(max-width:720px)').matches) host.querySelector('.pi-pane')?.scrollIntoView?.({ block: 'nearest' }); }; });
  }

  load();
  return { dispose() { disposed = true; }, refresh: () => refresh() };
}
