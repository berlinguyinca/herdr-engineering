// The Team Map for one project (unified workspace P06): the Work Graph drawn as columns (project, issue, task, agent, deliverable)
// with labelled lines, plus the same map as sentences. All logic lives in team-map-model.js; this file turns it into markup and
// wires the controls. Everything here is plain functions returning HTML so it runs under node; mountTeamMap adds the events.
//
// Gating: the graph routes answer 404 until control_plane.unified_workspace.enabled. A 404 is shown as a plain "not turned on"
// state (the server also answers 404 for a project that is hidden or does not exist, so the wording covers both), never an error.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { entityHref } from './entity-links.js';
import { skeleton } from './motion.js';
import { buildTeamMap, describeTeamMap, effectiveLayout, layoutNodes, nodeName, CELL, EDGE_MEANINGS, NOISY_KINDS } from './team-map-model.js';

const text = (v) => (typeof v === 'string' ? v : v == null ? '' : String(v));
const list = (v) => (Array.isArray(v) ? v : []);
const flat = (t, out = []) => { out.push(t); for (const c of t.children) flat(c, out); return out; };

/** Most nodes drawn at once; the text list always has all of them. */
export const MAX_DRAWN = 300;
export const NODE_BOX = { w: CELL.w - 24, h: CELL.h - 16 };

export const rootRef = (value) => { const v = text(value).trim(); return v ? (v.includes(':') ? v : `project:${v}`) : ''; };

const DONE = new Set(['done', 'closed', 'merged', 'completed', 'succeeded', 'resolved']);
const BAD = new Set(['blocked', 'failed', 'error']);
const LIVE = new Set(['active', 'running', 'in_progress', 'started', 'open_pr', 'review']);
/** A node's tone class; the status word is always printed too, so colour is never the only signal. */
export const toneOf = (status) => { const s = text(status).toLowerCase(); return DONE.has(s) ? 'ok' : BAD.has(s) ? 'failed' : LIVE.has(s) ? 'active' : 'waiting'; };

// ---- positions ----------------------------------------------------------------------------------------------------------
/** Saved (shared, then personal) positions are kept exactly; unplaced nodes are added without touching anything already in `prior`. */
export function placeNodes(nodes, { shared = {}, mine = {}, prior = {} } = {}) {
  const placed = { ...shared, ...mine, ...prior };
  const auto = layoutNodes(nodes, placed);
  const eff = effectiveLayout(auto, shared, mine);
  const out = { ...prior };
  for (const n of nodes) if (!out[n.id]) out[n.id] = eff[n.id];
  return out;
}

/** Move one node by whole cells. Refused (null) off the canvas or onto another node, so a move never displaces anything. */
export function moveNode(positions, id, dx, dy) {
  const p = positions[id];
  if (!p) return null;
  const to = { x: p.x + dx * CELL.w, y: p.y + dy * CELL.h };
  if (to.x < 0 || to.y < 0) return null;
  for (const [other, q] of Object.entries(positions)) if (other !== id && Math.abs(q.x - to.x) < CELL.w / 2 && Math.abs(q.y - to.y) < CELL.h / 2) return null;
  return { ...positions, [id]: to };
}

/**
 * The personal layout document to save: every entry already saved stays exactly as it is, except nodes the person moved; nodes
 * with no saved position at all (new work) get the position they were drawn at, so they stop depending on what else is shown.
 */
export function layoutToSave(mineDoc, positions, { shared = {}, moved = new Set() } = {}) {
  const nodes = { ...(mineDoc?.nodes || {}) };
  for (const [id, p] of Object.entries(positions)) {
    if (moved.has(id) || (!nodes[id] && !shared[id])) nodes[id] = { x: p.x, y: p.y };
  }
  return { ...(mineDoc || {}), nodes };
}

// ---- loading / saving through the real client --------------------------------------------------------------------------------
/** { status: 'ok' | 'off' | 'unavailable' | 'error', ... }. A layout that cannot be read does not stop the map. */
export async function loadTeamMap(client, root, { signal, depth = 3, maxNodes = 400 } = {}) {
  let snapshot;
  try { snapshot = await client.workGraphSnapshot(root, { depth, maxNodes, signal }); }
  catch (err) {
    if (err?.status === 404) return { status: 'off' };
    if (err?.status === 503) return { status: 'unavailable', message: text(err.message) };
    return { status: 'error', message: text(err?.message) || 'request failed' };
  }
  const read = async (which) => { try { return await client.workGraphLayout(root, { which, signal }); } catch { return null; } };
  const [shared, mine] = await Promise.all([read('default'), read('mine')]);
  return {
    status: 'ok', snapshot,
    shared: shared?.document?.nodes || {},
    mine: { doc: mine?.document || { nodes: {} }, etag: text(mine?.etag) },
    layoutReadable: Boolean(shared && mine),
  };
}

/** Saves the personal layout. Resolves { ok: true, mine } or { ok: false, message }; a stale etag asks for a reload, it never overwrites. */
export async function saveTeamMapLayout(client, root, { mine, positions, shared, moved }) {
  const document = layoutToSave(mine.doc, positions, { shared, moved });
  try {
    const saved = await client.saveWorkGraphLayout(root, document, { shared: false, ifMatch: mine.etag });
    return { ok: true, mine: { doc: saved?.document || document, etag: text(saved?.etag) } };
  } catch (err) {
    const message = err?.status === 412 ? 'Your layout changed somewhere else. Reload the map, then save again.'
      : err?.status === 404 ? 'Your layout could not be saved here (sign in as a person to keep a personal layout).'
        : `Your layout could not be saved: ${text(err?.message) || 'request failed'}`;
    return { ok: false, message };
  }
}

// ---- markup -----------------------------------------------------------------------------------------------------------------
/** The map for the current filters: what is drawn, and the sentences for it. Pure. */
export function viewModel(snapshot, filters = {}) {
  const map = buildTeamMap(snapshot, {
    kinds: filters.kind ? new Set([filters.kind]) : null, status: filters.status || null, query: filters.query || '', showExecution: Boolean(filters.showExecution),
  });
  const trees = [...map.groups, ...map.unplaced].flatMap((t) => flat(t));
  const text_ = describeTeamMap(map);
  const hierarchy = (e) => e.type === 'contains';
  const relationIds = new Set(map.edges.filter((e) => !hierarchy(e)).map((e) => e.id));
  return {
    map, trees, nodes: trees.map((t) => t.node),
    lines: text_.lines, relations: text_.relations.filter((r) => relationIds.has(r.id)),
    edges: map.edges.filter((e) => !hierarchy(e)),
  };
}

const short = (s, n = 26) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

export function nodeLabel(t) {
  const n = t.node;
  const bits = [`${n.kind} ${nodeName(n)}`, n.status ? `status ${n.status}` : '', t.progress?.total ? t.progress.milestones : '', t.deliverables ? `${t.deliverables} deliverable${t.deliverables === 1 ? '' : 's'}` : ''];
  return `${bits.filter(Boolean).join(', ')}. Enter shows details. Alt with an arrow key moves it.`;
}

export function renderCanvas(vm, positions, { selected = '', focusId = '' } = {}) {
  const drawn = vm.trees.slice(0, MAX_DRAWN).filter((t) => positions[t.node.id]);
  if (!drawn.length) return '';
  const ids = new Set(drawn.map((t) => t.node.id));
  const maxX = Math.max(...drawn.map((t) => positions[t.node.id].x)) + CELL.w;
  const maxY = Math.max(...drawn.map((t) => positions[t.node.id].y)) + CELL.h;
  const tabId = ids.has(focusId) ? focusId : ids.has(selected) ? selected : drawn[0].node.id;
  const edges = vm.edges.filter((e) => ids.has(e.from_id) && ids.has(e.to_id)).map((e) => {
    const a = positions[e.from_id], b = positions[e.to_id];
    const x1 = a.x + NODE_BOX.w, y1 = a.y + NODE_BOX.h / 2, x2 = b.x, y2 = b.y + NODE_BOX.h / 2;
    return `<line class="tm-edge tm-edge--${esc(e.meaning)}${e.live === false ? ' tm-edge--past' : ''}" data-edge="${esc(e.id)}" data-meaning="${esc(e.meaning)}" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" marker-end="url(#tm-arrow)"><title>${esc(EDGE_MEANINGS[e.meaning])}${e.live === false ? ' (earlier, no longer current)' : ''}</title></line>`;
  }).join('');
  const nodes = drawn.map((t) => {
    const n = t.node, p = positions[n.id];
    const sub = t.progress?.total ? t.progress.milestones : text(n.status);
    return `<g class="tm-node tm-node--${toneOf(n.status)}" data-node-id="${esc(n.id)}" data-kind="${esc(n.kind)}" role="button" tabindex="${n.id === tabId ? 0 : -1}" aria-pressed="${n.id === selected}" aria-label="${esc(nodeLabel(t))}" transform="translate(${p.x} ${p.y})">`
      + `<rect width="${NODE_BOX.w}" height="${NODE_BOX.h}" rx="6"/><text class="tm-kind" x="10" y="17">${esc(n.kind)}</text><text class="tm-name" x="10" y="35">${esc(short(nodeName(n)))}</text><text class="tm-sub" x="10" y="51">${esc(short(sub, 30))}</text></g>`;
  }).join('');
  return `<svg class="tm-svg" data-tm-canvas width="${maxX}" height="${maxY}" viewBox="0 0 ${maxX} ${maxY}" role="group" aria-label="Team map picture. The same map is written out below.">`
    + `<defs><marker id="tm-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" class="tm-arrowhead"/></marker></defs>${edges}${nodes}</svg>`
    + (vm.trees.length > MAX_DRAWN ? `<p class="tm-note" data-tm-capped>Showing the first ${MAX_DRAWN} of ${vm.trees.length} things. Use the filters to narrow the picture; the written map lists everything.</p>` : '');
}

const LEGEND = ['coordination', 'delegation', 'dependency', 'produced', 'reviewed'];
export const renderLegend = () => `<ul class="tm-legend" aria-label="What the lines mean">${LEGEND.map((m) => `<li><svg width="36" height="10" aria-hidden="true"><line class="tm-edge tm-edge--${m}" x1="1" y1="5" x2="35" y2="5"/></svg> ${esc(EDGE_MEANINGS[m])}</li>`).join('')}<li><svg width="36" height="10" aria-hidden="true"><line class="tm-edge tm-edge--past" x1="1" y1="5" x2="35" y2="5"/></svg> earlier, no longer current</li></ul>`;

export function renderProgress(vm) {
  const rows = vm.map.groups.filter((g) => g.progress && g.progress.total);
  if (!rows.length) return '';
  return `<ul class="tm-progress" aria-label="Progress by milestone">${rows.map((g) => `<li data-tm-progress><strong>${esc(nodeName(g.node))}</strong> — ${esc(g.progress.milestones)} <span class="tm-chip">${esc(g.progress.label)}</span></li>`).join('')}</ul>`;
}

export function renderText(vm) {
  if (!vm.lines.length) return '';
  const lines = vm.lines.map((l) => `<li class="tm-line" data-depth="${l.depth}" data-text-node="${esc(l.id)}"><button type="button" class="tm-linkbtn" data-open-node="${esc(l.id)}">${esc(l.text)}</button></li>`).join('');
  const rel = vm.relations.length
    ? `<h3>How things relate</h3><ul class="tm-relations" data-tm-relations>${vm.relations.map((r) => `<li data-live="${r.live}">${esc(r.text)}</li>`).join('')}</ul>` : '';
  return `<details class="tm-text" data-tm-text open><summary>Written map (same content as the picture)</summary><h3>Work, grouped</h3><ul class="tm-lines" data-tm-lines>${lines}</ul>${rel}</details>`;
}

export function renderInspector(vm, id) {
  const t = vm.trees.find((x) => x.node.id === id);
  if (!t) return '<aside class="tm-inspector" data-tm-inspector aria-live="polite" hidden></aside>';
  const n = t.node;
  const related = vm.relations.filter((r) => r.text.includes(nodeName(n)));
  const refKind = { artifact: 'artifact', artifact_version: 'artifact', agent_session: 'session', host: 'host', review: 'review' }[n.kind];
  const href = refKind && n.native_id ? entityHref(refKind, n.native_id) : null;
  return `<aside class="tm-inspector" data-tm-inspector aria-live="polite"><h3>${esc(nodeName(n))}</h3><dl><dt>Kind</dt><dd>${esc(n.kind)}</dd><dt>Status</dt><dd>${esc(text(n.status) || 'not recorded')}</dd>`
    + (t.progress?.total ? `<dt>Progress</dt><dd>${esc(t.progress.milestones)}</dd>` : '')
    + (t.deliverables ? `<dt>Deliverables</dt><dd>${t.deliverables}</dd>` : '')
    + `<dt>Id</dt><dd><code>${esc(n.id)}</code></dd></dl>`
    + (related.length ? `<ul>${related.map((r) => `<li>${esc(r.text)}</li>`).join('')}</ul>` : '')
    + (href ? `<p><a href="${esc(href)}" data-tm-open-link>Open in its own page</a></p>` : '') + '</aside>';
}

const sel = (name, label, options, value) => `<label>${label}<select data-tm-filter="${name}"><option value="">All</option>${options.map((o) => `<option value="${esc(o)}"${o === value ? ' selected' : ''}>${esc(o)}</option>`).join('')}</select></label>`;

export function renderBar(snapshot, filters, rootValue) {
  const nodes = list(snapshot?.nodes).filter((n) => n && !n.tombstone);
  const kinds = [...new Set(nodes.map((n) => text(n.kind)).filter(Boolean))].sort();
  const statuses = [...new Set(nodes.map((n) => text(n.status)).filter(Boolean))].sort();
  const execution = nodes.filter((n) => NOISY_KINDS.has(n.kind)).length;
  return `<form class="tm-bar" data-tm-root-form role="search"><label>Project<input type="text" data-tm-root value="${esc(rootValue)}" placeholder="project:name" autocomplete="off"></label><button type="submit" class="tm-btn">Open</button></form>`
    + (snapshot ? `<div class="tm-bar" role="group" aria-label="Filters"><label>Search<input type="search" data-tm-filter="query" value="${esc(filters.query || '')}"></label>${sel('kind', 'Kind', kinds, filters.kind)}${sel('status', 'Status', statuses, filters.status)}`
      + `<label class="tm-check"><input type="checkbox" data-tm-filter="showExecution"${filters.showExecution ? ' checked' : ''}> Show agent sessions and hosts${execution ? ` (${execution} folded away)` : ''}</label></div>` : '');
}

/** The whole body for a state. Pure so the states can be tested without a page. */
export function renderBody(state) {
  const msg = (kind, html) => `<p class="tm-state" data-tm-state="${kind}" role="status">${html}</p>`;
  if (state.phase === 'idle') return msg('idle', 'Enter a project (for example <code>project:atlas</code>) to see its team map.');
  if (state.phase === 'loading') return skeleton('team map', { rows: 3, height: 64 });
  if (state.phase === 'off') return msg('off', 'The Team Map is not turned on for this control plane, or that project cannot be found. Nothing is wrong with your account or this page.');
  if (state.phase === 'unavailable') return msg('unavailable', `The work graph is not available right now${state.message ? ` (${esc(state.message)})` : ''}. Try again shortly.`);
  if (state.phase === 'error') return msg('error', `The Team Map could not be loaded: ${esc(state.message || 'request failed')}`);
  const vm = state.vm;
  if (!list(state.snapshot?.nodes).filter((n) => n && !n.tombstone).length) return msg('empty', 'Nothing is recorded for this project yet. Tasks, deliverables and reviews appear here as soon as they are linked to it.');
  const notes = [state.snapshot?.has_more ? '<p class="tm-note" data-tm-truncated>This is part of a larger map. Narrow it with the filters.</p>' : '',
    state.layoutReadable === false ? '<p class="tm-note" data-tm-layout-note>Saved positions could not be read, so positions are automatic and cannot be saved now.</p>' : ''].join('');
  if (!vm.nodes.length) return `${notes}${msg('nomatch', `No item matches these filters. <button type="button" class="tm-linkbtn" data-tm-clear>Clear filters</button>`)}`;
  const save = state.layoutReadable === false ? '' : `<div class="tm-save"><button type="button" class="tm-btn" data-tm-save>Save my layout</button> <span data-tm-save-msg role="status">${esc(state.saveMsg || (state.moved.size ? `${state.moved.size} moved, not saved yet.` : ''))}</span></div>`;
  return `${notes}${renderProgress(vm)}${renderLegend()}<div class="tm-canvas">${renderCanvas(vm, state.positions, { selected: state.selected, focusId: state.focusId })}</div>${save}${renderInspector(vm, state.selected)}${renderText(vm)}`;
}

// ---- the page ------------------------------------------------------------------------------------------------------------
export function mountTeamMap(container, client, { root = '', freshness = () => null, onRoot = () => {} } = {}) {
  const state = { phase: root ? 'loading' : 'idle', root: rootRef(root), snapshot: null, shared: {}, mine: { doc: { nodes: {} }, etag: '' }, layoutReadable: true,
    filters: { query: '', kind: '', status: '', showExecution: false }, positions: {}, moved: new Set(), selected: '', focusId: '', saveMsg: '', vm: null, message: '' };
  let disposed = false, controller = null;
  container.innerHTML = '<section class="tm" data-team-map><div data-tm-bar></div><div data-tm-body></div></section>';
  const barEl = container.querySelector('[data-tm-bar]'), bodyEl = container.querySelector('[data-tm-body]');

  const recompute = () => {
    state.vm = state.snapshot ? viewModel(state.snapshot, state.filters) : null;
    if (state.vm) state.positions = placeNodes(state.vm.nodes, { shared: state.shared, mine: state.mine.doc.nodes, prior: state.positions });
  };
  const paintBar = () => { barEl.innerHTML = renderBar(state.snapshot, state.filters, state.root); };
  const paintBody = () => {
    bodyEl.innerHTML = renderBody(state);
    if (state.focusId) { bodyEl.querySelector(`[data-node-id="${CSS.escape(state.focusId)}"]`)?.focus({ preventScroll: false }); state.focusId = ''; }
  };

  async function load() {
    if (disposed) return;
    controller?.abort();
    const request = controller = new AbortController();
    if (!state.root) { state.phase = 'idle'; paintBar(); paintBody(); return; }
    state.phase = 'loading'; paintBody();
    const signal = AbortSignal.any([request.signal, AbortSignal.timeout(20000)]);
    const result = await loadTeamMap(client, state.root, { signal });
    if (disposed || request.signal.aborted) return;
    if (result.status === 'ok') {
      Object.assign(state, { phase: 'ok', snapshot: result.snapshot, shared: result.shared, mine: result.mine, layoutReadable: result.layoutReadable, moved: new Set(), saveMsg: '' });
      recompute();
      freshness()?.touch(true);
    } else {
      Object.assign(state, { phase: result.status, message: result.message || '', snapshot: null, vm: null });
      // "Not enabled / not found" is an answer, not a failure (and a mistyped project must not hide the nav entry as "unavailable").
      if (result.status === 'off') freshness()?.touch(true); else freshness()?.touch(false, result.message || result.status);
    }
    paintBar(); paintBody();
  }

  const order = () => (state.vm ? state.vm.trees.slice(0, MAX_DRAWN).map((t) => t.node.id) : []);
  const select = (id, focus) => { state.selected = id; if (focus) state.focusId = id; paintBody(); };

  container.addEventListener('submit', (event) => {
    if (!event.target.matches('[data-tm-root-form]')) return;
    event.preventDefault();
    state.root = rootRef(container.querySelector('[data-tm-root]').value);
    state.positions = {}; state.selected = ''; state.snapshot = null; onRoot(state.root);
    void load();
  });
  const onFilter = (event) => {
    const el = event.target.closest?.('[data-tm-filter]');
    if (!el) return;
    state.filters[el.dataset.tmFilter] = el.type === 'checkbox' ? el.checked : el.value;
    recompute(); paintBody();
  };
  container.addEventListener('input', (event) => { if (event.target.type === 'search') onFilter(event); });
  container.addEventListener('change', (event) => { if (event.target.type !== 'search') onFilter(event); });
  container.addEventListener('click', async (event) => {
    const node = event.target.closest('[data-node-id]'), open = event.target.closest('[data-open-node]');
    if (node) return select(node.dataset.nodeId, true);
    if (open) return select(open.dataset.openNode, false);
    if (event.target.closest('[data-tm-clear]')) { state.filters = { query: '', kind: '', status: '', showExecution: false }; recompute(); paintBar(); return paintBody(); }
    if (event.target.closest('[data-tm-save]')) {
      state.saveMsg = 'Saving…'; paintBody();
      const res = await saveTeamMapLayout(client, state.root, { mine: state.mine, positions: state.positions, shared: state.shared, moved: state.moved });
      if (disposed) return;
      if (res.ok) { state.mine = res.mine; state.moved = new Set(); state.saveMsg = 'Layout saved.'; } else state.saveMsg = res.message;
      paintBody();
    }
  });
  container.addEventListener('keydown', (event) => {
    const g = event.target.closest?.('[data-node-id]');
    if (!g) return;
    const id = g.dataset.nodeId, ids = order(), i = ids.indexOf(id);
    const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); return select(id, true); }
    if (event.altKey && arrows[event.key]) {
      event.preventDefault();
      const next = moveNode(state.positions, id, ...arrows[event.key]);
      if (!next) { state.saveMsg = 'That spot is taken or off the map.'; state.focusId = id; return paintBody(); }
      state.positions = next; state.moved.add(id); state.saveMsg = ''; state.focusId = id; return paintBody();
    }
    const step = { ArrowUp: -1, ArrowLeft: -1, ArrowDown: 1, ArrowRight: 1 }[event.key];
    const target = event.key === 'Home' ? ids[0] : event.key === 'End' ? ids[ids.length - 1] : step ? ids[i + step] : undefined;
    if (target) { event.preventDefault(); g.setAttribute('tabindex', '-1'); const el = container.querySelector(`[data-node-id="${CSS.escape(target)}"]`); el?.setAttribute('tabindex', '0'); el?.focus(); }
  });

  paintBar(); paintBody();
  void load();
  return { refresh: load, dispose() { disposed = true; controller?.abort(); } };
}
