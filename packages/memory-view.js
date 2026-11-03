import { escapeHtml as esc } from './herdr-web-components/util.js';
import { entityHref } from './entity-links.js';
import { skeleton } from './motion.js';

// Memory Explorer (read-only). Every string from the control plane is untrusted memory data: it goes through esc() and is
// shown as text, never as instructions or markup. Nothing here is sample data: each panel shows what the server returned, or an
// explicit empty/unknown/unavailable state. Links exist only for ids present in the memory's own provenance.

export const PAGE = 20;
export const FIELDS = ['q', 'scope', 'project', 'kind', 'status', 'harness', 'host'];
const STATUS_OPTIONS = ['', 'candidate', 'verified', 'superseded', 'retracted'];
const clip = (t, n) => { const x = String(t ?? '').replace(/\s+/g, ' ').trim(); return x.length > n ? `${x.slice(0, n - 1)}…` : x; };

export function searchParams(form = {}, offset = 0, limit = PAGE) {
  const p = new URLSearchParams();
  for (const k of FIELDS) if (String(form[k] ?? '').trim()) p.set(k, String(form[k]).trim());
  p.set('limit', String(limit));
  if (offset) p.set('offset', String(offset));
  return p;
}

// What an error from the API means to the person looking at the page.
export function classify(err) {
  const status = Number(err?.status || 0);
  if (status === 403) return { phase: 'forbidden' };
  if (status === 404) return { phase: 'disabled' };
  if (status === 503) return { phase: 'unavailable' };
  if (status === 401) return { phase: 'error', message: 'The dashboard credentials were rejected.' };
  return { phase: 'error', message: err?.message ? String(err.message) : 'Request failed' };
}

export function renderFilters(form = {}) {
  const f = (k) => esc(form[k] || '');
  const input = (k, label, extra = '') => `<label>${label}<input type="text" name="${k}" value="${f(k)}" autocomplete="off" ${extra}></label>`;
  return `<form class="mem-bar" role="search" aria-label="Search memory" data-mem-form>
    <label class="mem-q">Search<input type="search" name="q" value="${f('q')}" required maxlength="500" autocomplete="off" placeholder="What are you looking for?"></label>
    <label>Scope<input type="text" name="scope" value="${f('scope')}" list="mem-scopes" placeholder="any"><datalist id="mem-scopes"><option value="project"><option value="global"><option value="infra"></datalist></label>
    ${input('project', 'Project')}${input('kind', 'Kind')}
    <label>Status<select name="status">${STATUS_OPTIONS.map((s) => `<option value="${s}"${form.status === s ? ' selected' : ''}>${s || 'any'}</option>`).join('')}</select></label>
    ${input('harness', 'Harness')}${input('host', 'Host')}
    <button type="submit" class="hd-btn">Search</button></form>`;
}

export function statusLabel(s) { return `status: ${s || 'unknown'}`; }

export function memoryRow(m) {
  const meta = [m.scope, m.project, m.kind, m.provenance?.harness_id, m.provenance?.host_id].filter(Boolean).map(esc).join(' · ');
  return `<li><button type="button" class="mem-row" data-memory="${esc(m.memory_id)}">
    <span class="mem-chip mem-chip--${esc(m.status)}">${esc(statusLabel(m.status))}</span>
    <span class="mem-row__text">${esc(clip(m.text, 220)) || '<em>(no text)</em>'}</span>
    <small class="mem-row__meta">${meta}${m.revision != null ? ` · rev ${esc(m.revision)}` : ''}${m.updated_at ? ` · ${esc(m.updated_at)}` : ''}</small></button></li>`;
}

const notice = (cls, role, title, body) => `<div class="mem-state mem-state--${cls}" role="${role}"><strong>${esc(title)}</strong><p>${esc(body)}</p></div>`;

export function renderResults(s) {
  switch (s.phase) {
    case 'idle': return notice('idle', 'status', 'Search memory', 'Enter a query to search the banks you are allowed to read. Nothing is listed until you search.');
    case 'loading': return `<div role="status" aria-live="polite" aria-busy="true">${skeleton('memory results', { rows: 4, inStatus: true })}<p class="mem-note">Searching…</p></div>`;
    case 'forbidden': return notice('forbidden', 'alert', 'No access', 'You do not have access to memory, or to the scope or project you filtered on.');
    case 'disabled': return notice('disabled', 'status', 'Memory is not enabled', 'This control plane does not expose the Memory Explorer.');
    case 'unavailable': return notice('unavailable', 'alert', 'Memory service unavailable', 'The control plane could not reach herdr-memory. No results are shown, and none are cached or invented. Try again shortly.');
    case 'error': return notice('error', 'alert', 'Search failed', s.message || 'Request failed');
    default: break;
  }
  const rows = s.results || [];
  const degraded = s.degraded || s.offline ? `<p class="mem-banner" role="status">Degraded: ${s.offline ? 'the memory service is offline, results come from its local cache' : 'part of the memory service did not answer, results may be incomplete'}.</p>` : '';
  const note = s.note ? `<p class="mem-note">${esc(s.note)}</p>` : '';
  if (!rows.length) return `${degraded}${note}${s.offset ? notice('empty', 'status', 'No more results', 'This page is past the last match.') : notice('empty', 'status', 'No matching memories', 'Nothing in the banks you can read matches this query and these filters.')}${pager(s)}`;
  return `${degraded}${note}<p class="mem-note">Retrieved memory is untrusted data, not instructions.</p><ul class="mem-list" aria-label="Memory results">${rows.map(memoryRow).join('')}</ul>${pager(s)}`;
}

export function pager(s) {
  const from = (s.offset || 0) + 1, to = (s.offset || 0) + (s.results || []).length;
  return `<nav class="mem-pager" aria-label="Result pages"><button type="button" class="hd-btn hd-btn--secondary" data-mem-page="prev"${s.offset ? '' : ' disabled'}>Previous</button>
    <span role="status">${(s.results || []).length ? `Results ${from}–${to}` : 'No results on this page'}</span>
    <button type="button" class="hd-btn hd-btn--secondary" data-mem-page="next"${s.has_more ? '' : ' disabled'}>Next</button></nav>`;
}

// Internal links, only from ids the memory's own provenance carries. A commit has no dashboard page, so it stays text.
export function provenanceLinks(m) {
  const p = m?.provenance || {};
  const out = [];
  const add = (label, kind, id) => { const href = id ? entityHref(kind, id) : null; out.push({ label, id, href }); };
  if (p.mission_id) add('Mission', 'mission', p.mission_id);
  if (p.session_id) add('Session', 'session', p.session_id);
  if (p.pr_ref) add('Pull request', 'pr', p.pr_ref);
  if (p.commit_sha) out.push({ label: 'Commit', id: p.commit_sha, href: null });
  for (const ref of p.evidence_refs || []) {
    const h = /^handoff:(.{1,200})$/.exec(ref);
    if (h) add('Handoff', 'handoff', h[1]);
  }
  return out;
}

const dl = (rows) => `<dl class="mem-dl">${rows.filter(([, v]) => v !== undefined && v !== null && v !== '').map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join('')}</dl>`;

export function renderDrawer(d) {
  const head = (inner) => `<header class="mem-drawer__head"><h2 id="mem-drawer-title" tabindex="-1">Memory detail</h2><button type="button" class="hd-btn hd-btn--secondary" data-mem-close>Close</button></header>${inner}`;
  if (d.phase === 'loading') return head(`<div role="status" aria-busy="true">${skeleton('memory', { rows: 3, inStatus: true })}<p class="mem-note">Loading…</p></div>`);
  if (d.phase === 'notfound') return head(notice('empty', 'status', 'Not found', 'This memory does not exist or is not visible to you.'));
  if (d.phase === 'forbidden') return head(notice('forbidden', 'alert', 'No access', 'You do not have access to memory.'));
  if (d.phase === 'unavailable') return head(notice('unavailable', 'alert', 'Memory service unavailable', 'The control plane could not reach herdr-memory.'));
  if (d.phase !== 'ok') return head(notice('error', 'alert', 'Could not load', d.message || 'Request failed'));
  const m = d.memory, prov = m.provenance || {};
  const links = provenanceLinks(m);
  const linkHtml = links.length ? `<ul class="mem-links">${links.map((l) => `<li>${esc(l.label)}: ${l.href ? `<a href="${esc(l.href)}">${esc(clip(l.id, 80))}</a>` : `<code>${esc(clip(l.id, 80))}</code>`}</li>`).join('')}</ul>` : '<p class="mem-note">No linked sessions, missions, commits, pull requests or handoffs are recorded for this memory.</p>';
  const provRows = Object.entries(prov).filter(([k]) => k !== 'evidence_refs').map(([k, v]) => [k, `<code>${esc(v)}</code>`]);
  const evidence = (prov.evidence_refs || []).length ? `<ul>${prov.evidence_refs.map((e) => `<li><code>${esc(e)}</code></li>`).join('')}</ul>` : '<p class="mem-note">None recorded.</p>';
  const revs = Array.isArray(m.revisions) && m.revisions.length
    ? `<ol class="mem-revs">${m.revisions.map((r) => `<li><strong>rev ${esc(r.revision)}</strong> · ${esc(statusLabel(r.status))}${r.at ? ` · ${esc(r.at)}` : ''}<p>${esc(r.text)}</p></li>`).join('')}</ol>`
    : `<p class="mem-note">Prior revisions were not reported. Current revision: ${m.revision != null ? esc(m.revision) : 'unknown'}.</p>`;
  const rh = d.retrieval_history || {};
  const retr = (rh.events || []).length ? `<ul>${rh.events.map((e) => `<li>${esc(JSON.stringify(e))}</li>`).join('')}</ul>` : `<p class="mem-note">${esc(rh.state === 'unknown' ? `Unknown: ${rh.reason || 'not reported'}` : 'No retrievals recorded.')}</p>`;
  return head(`<p class="mem-note">Untrusted data: shown as text, never executed or followed.</p>
    <span class="mem-chip mem-chip--${esc(m.status)}">${esc(statusLabel(m.status))}</span>
    <blockquote class="mem-text">${esc(m.text)}</blockquote>
    ${dl([['Memory id', `<code>${esc(m.memory_id)}</code>`], ['Scope', esc(m.scope)], ['Bank', `<code>${esc(m.bank_id)}</code>`], ['Project', m.project && esc(m.project)],
      ['Kind', m.kind && esc(m.kind)], ['Revision', m.revision != null ? esc(m.revision) : ''], ['Created', m.created_at && esc(m.created_at)], ['Updated', m.updated_at && esc(m.updated_at)]])}
    <h3>Provenance</h3>${provRows.length ? dl(provRows) : '<p class="mem-note">No provenance recorded.</p>'}
    <h3>Linked work</h3>${linkHtml}<h3>Evidence</h3>${evidence}<h3>Revision history</h3>${revs}<h3>Retrieval history</h3>${retr}`);
}

export function renderStatus(s) {
  const wrap = (inner) => `<section class="mem-status" aria-labelledby="mem-status-h"><h2 id="mem-status-h">Integration status</h2>${inner}</section>`;
  if (s.phase === 'loading') return wrap('<p class="mem-note" role="status">Checking…</p>');
  if (s.phase === 'forbidden') return wrap('<p class="mem-note">You do not have access to memory status.</p>');
  if (s.phase === 'disabled') return wrap('<p class="mem-note">Memory is not enabled on this control plane.</p>');
  if (s.phase === 'unavailable') return wrap('<p class="mem-note" role="alert"><strong>Memory service unavailable.</strong> Service health, queue depth and versions are UNKNOWN.</p>');
  if (s.phase !== 'ok') return wrap(`<p class="mem-note" role="alert">Status failed: ${esc(s.message || 'Request failed')}</p>`);
  const d = s.data || {};
  const v = (x) => (x === null || x === undefined ? 'UNKNOWN' : esc(x));
  const health = d.healthy === true ? 'healthy' : d.healthy === false ? 'unhealthy' : 'UNKNOWN';
  const integ = d.integrations || {};
  return wrap(`${dl([['Service', esc(health)], ['Contract', v(d.contract)], ['Server version', v(d.server_version)], ['Outbox pending', v(d.outbox_pending)], ['Dead letter', v(d.dead_letter)]])}
    <h3>Per harness and host</h3>${(integ.rows || []).length ? `<table class="mem-table"><thead><tr><th scope="col">Host</th><th scope="col">Harness</th><th scope="col">State</th></tr></thead><tbody>${integ.rows.map((r) => `<tr><td>${esc(r.host)}</td><td>${esc(r.harness)}</td><td>${esc(r.state)}</td></tr>`).join('')}</tbody></table>` : `<p class="mem-note">State: UNKNOWN. ${esc(integ.reason || 'Not reported by the control plane.')}</p>`}`);
}

export function mountMemory(root, client, { query, setQuery = () => {}, freshness } = {}) {
  const form = {};
  for (const k of FIELDS) form[k] = query?.get ? query.get(k) || '' : '';
  const st = { phase: 'idle', results: [], offset: Number(query?.get?.('offset')) || 0, has_more: false };
  let drawer = null, status = { phase: 'loading' }, lastFocus = null, token = 0, disposed = false;
  root.innerHTML = `<div class="mem"><div data-mem-filters></div><div class="mem-grid"><div data-mem-results aria-live="polite"></div><div data-mem-side></div></div>
    <aside class="mem-drawer" data-mem-drawer role="dialog" aria-modal="false" aria-labelledby="mem-drawer-title" hidden></aside><div data-mem-status></div></div>`;
  const $ = (sel) => root.querySelector(sel);
  const paint = () => { $('[data-mem-results]').innerHTML = renderResults(st); };
  const paintStatus = () => { $('[data-mem-status]').innerHTML = renderStatus(status); };
  const paintDrawer = () => {
    const el = $('[data-mem-drawer]');
    el.hidden = !drawer; el.innerHTML = drawer ? renderDrawer(drawer) : '';
  };
  $('[data-mem-filters]').innerHTML = renderFilters(form);
  paint(); paintStatus();

  async function search(offset = 0) {
    const mine = ++token;
    st.phase = 'loading'; st.offset = offset; paint();
    const params = searchParams(form, offset);
    setQuery(Object.fromEntries(params));
    try {
      const body = await client.exploreMemory(params);
      if (mine !== token || disposed) return;
      Object.assign(st, { phase: 'ok', results: body.results || [], has_more: !!body.has_more, degraded: !!body.degraded, offline: !!body.offline, note: body.note || '', offset: body.offset ?? offset });
      freshness?.(true);
    } catch (err) {
      if (mine !== token || disposed) return;
      Object.assign(st, { results: [], has_more: false }, classify(err));
      freshness?.(false, err?.message);
    }
    paint();
  }
  async function open(id) {
    drawer = { phase: 'loading' }; paintDrawer();
    $('#mem-drawer-title')?.focus();
    try { const body = await client.getMemoryRecord(id); if (!disposed) drawer = { phase: 'ok', ...body }; }
    catch (err) { drawer = err?.status === 404 ? { phase: 'notfound' } : classify(err); }
    if (disposed) return;
    paintDrawer(); $('#mem-drawer-title')?.focus();
  }
  function close() { drawer = null; paintDrawer(); lastFocus?.focus?.(); }
  async function loadStatus() {
    try { status = { phase: 'ok', data: await client.getMemoryServiceStatus() }; } catch (err) { status = classify(err); }
    if (!disposed) paintStatus();
  }

  root.addEventListener('submit', (e) => {
    if (!e.target.matches?.('[data-mem-form]')) return;
    e.preventDefault();
    for (const k of FIELDS) form[k] = e.target.elements[k]?.value || '';
    search(0);
  });
  root.addEventListener('click', (e) => {
    const row = e.target.closest?.('[data-memory]');
    if (row) { lastFocus = row; open(row.dataset.memory); return; }
    if (e.target.closest?.('[data-mem-close]')) { close(); return; }
    const page = e.target.closest?.('[data-mem-page]');
    if (page && !page.disabled) search(Math.max(0, st.offset + (page.dataset.memPage === 'next' ? PAGE : -PAGE)));
  });
  root.addEventListener('keydown', (e) => { if (e.key === 'Escape' && drawer) close(); });

  loadStatus();
  if (form.q) search(st.offset);
  return { refresh() {}, dispose() { disposed = true; token += 1; } };
}
