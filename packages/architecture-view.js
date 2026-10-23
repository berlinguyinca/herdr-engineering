// Architecture tab: browse immutable commit-keyed code-graph snapshots. A client of
// /api/v1/architecture/*; renders loading / disabled / empty / error / stale / partial states.
import {escapeHtml as esc} from './herdr-web-components/util.js';
import {layoutGraph, NODE_W, NODE_H} from './architecture-layout.js';

const PAGE = 100;
const text = v => typeof v === 'string' ? v : '';

// Source links exist only for github.com repositories and full commit SHAs; otherwise path:line text.
export function sourceUrl(snapshot, node) {
  const repo = /^github\.com\/([\w.-]+\/[\w.-]+)$/.exec(text(snapshot?.repository_id))?.[1];
  const path = text(node?.relative_path);
  if (!repo || !path || !/^[a-f0-9]{40}([a-f0-9]{24})?$/.test(text(snapshot?.commit_sha))) return null;
  if (!(Number.isSafeInteger(node.line_start) && node.line_start > 0)) return null; // directories/packages have no blob line
  const line = `#L${node.line_start}`;
  return `https://github.com/${repo}/blob/${snapshot.commit_sha}/${path.split('/').map(encodeURIComponent).join('/')}${line}`;
}
export function snapshotNotices(snapshot, all = []) {
  const out = [];
  if (!snapshot) return out;
  if (snapshot.completeness !== 'complete') out.push({kind: 'partial', text: 'Partial snapshot: some languages or edge kinds are not analysed.'});
  const newer = all.find(s => s.repository_id === snapshot.repository_id && s.snapshot_id !== snapshot.snapshot_id && s.created_at > snapshot.created_at);
  if (newer) out.push({kind: 'stale', text: `Stale: a newer snapshot exists (${newer.commit_sha.slice(0, 8)}).`});
  return out;
}
const label = s => `${text(s.repository_id).replace(/^github\.com\//, '')} @ ${text(s.commit_sha).slice(0, 8)} · ${text(s.host_id)} · wt ${text(s.worktree_id).slice(0, 6)}`;

export function mountArchitecture(root, client, opts = {}) {
  let alive = true, ctl = new AbortController();
  const st = {snapshots: [], sid: opts.snapshot || '', parent: opts.parent || '', q: opts.q || '', page: null, detail: null, diff: null,
    analyses: [], cursor: null};
  root.innerHTML = '<div class="arch"><herdr-spinner></herdr-spinner></div>';
  const host = root.firstElementChild;
  const snap = () => st.snapshots.find(s => s.snapshot_id === st.sid);
  const fail = e => alive && (host.innerHTML = e.status === 404
    ? '<herdr-empty message="The Architecture Explorer is not enabled on this control plane (control_plane.architecture.enabled)."></herdr-empty>'
    : `<herdr-error message="${esc(e.message || 'request failed')}"></herdr-error>`);

  async function load() {
    try {
      const [s, a] = await Promise.all([client.listArchitectureSnapshots({limit: 200, signal: ctl.signal}),
        client.listArchitectureAnalyses({signal: ctl.signal}).catch(() => ({analyses: []}))]);
      if (!alive) return;
      st.snapshots = s.snapshots; st.analyses = a.analyses;
      if (!st.sid && st.snapshots[0]) st.sid = st.snapshots[0].snapshot_id;
      await browse();
    } catch (e) { if (e.name !== 'AbortError') fail(e); }
  }
  async function browse(cursor) {
    if (!snap()) return render();
    try {
      if (st.q) st.page = await client.searchArchitectureNodes(st.sid, {q: st.q, limit: PAGE, cursor, signal: ctl.signal});
      else st.page = await client.getArchitectureChildren(st.sid, {parent: st.parent || undefined, limit: PAGE, cursor, signal: ctl.signal});
      st.cursor = cursor || null;
      if (!alive) return; render();
    } catch (e) { if (e.name !== 'AbortError') fail(e); }
  }
  function graphSvg() {
    const p = st.page;
    if (!p || st.q || !p.nodes.length) return '';
    const L = layoutGraph(p.nodes.map(n => ({id: n.node_id, n})), p.edges.map(e => ({from: e.from_id, to: e.to_id, count: e.count, e})));
    const edges = L.edges.map(e => `<path class="arch-edge${e.back ? ' back' : ''}" d="${e.d}" marker-end="url(#arch-arrow)"><title>${esc(`${e.from} → ${e.to} (${e.count} import${e.count === 1 ? '' : 's'}${e.e.inferred ? ', inferred' : ''})`)}</title></path>`).join('');
    const nodes = L.nodes.map(v => `<g class="arch-node kind-${esc(v.n.kind)}" transform="translate(${v.x},${v.y})" tabindex="0" role="button" data-node="${esc(v.id)}" aria-label="${esc(`${v.n.kind} ${v.n.qualified_name}${v.n.child_count ? ', ' + v.n.child_count + ' children, press Enter to open' : ''}`)}"><rect width="${NODE_W}" height="${NODE_H}" rx="6"/><text x="10" y="19" class="t1">${esc(v.n.qualified_name.split('.').pop().slice(0, 24))}</text><text x="10" y="35" class="t2">${esc(v.n.kind)}${v.n.child_count ? ` · ${v.n.child_count}` : ''}</text></g>`).join('');
    return `<div class="arch-graph" data-testid="arch-graph"><svg viewBox="0 0 ${L.width} ${L.height}" width="${L.width}" height="${L.height}" role="group" aria-label="Dependency graph of the ${p.nodes.length} items below; the table lists the same items"><defs><marker id="arch-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L8,4 L0,8 z"/></marker></defs>${edges}${nodes}</svg></div>${L.hidden ? `<p class="arch-note">Showing the ${L.nodes.length} best-connected of ${p.nodes.length} items in the graph; the table has all of them.</p>` : ''}`;
  }
  function crumbs() {
    const d = st.detail?.ancestors || [];
    const items = [{id: '', name: 'repository'}, ...d.filter(a => a.kind !== 'repo').map(a => ({id: a.node_id, name: a.qualified_name.split('.').pop()}))];
    if (st.page?.parent && st.page.parent.kind !== 'repo') items.push({id: st.page.parent.node_id, name: st.page.parent.qualified_name.split('.').pop()});
    return `<nav class="arch-crumbs" aria-label="Location">${items.map((c, i) => `<button type="button" class="link" data-crumb="${esc(c.id)}"${i === items.length - 1 ? ' aria-current="location"' : ''}>${esc(c.name)}</button>`).join(' › ')}</nav>`;
  }
  function table() {
    const p = st.page;
    if (!p) return '';
    if (!p.nodes.length) return `<herdr-empty message="${st.q ? 'No matching symbols in this snapshot' : 'This node has no children'}"></herdr-empty>`;
    const rows = p.nodes.map(n => `<tr><td><button type="button" class="link" data-open="${esc(n.node_id)}">${esc(n.qualified_name)}</button></td><td>${esc(n.kind)}</td><td>${esc(n.relative_path || '')}${n.line_start ? `:${n.line_start}` : ''}</td><td>${n.child_count || ''}</td></tr>`).join('');
    return `<table class="arch-table"><caption>${p.total} item${p.total === 1 ? '' : 's'}${p.incomplete ? ' · more available' : ''}</caption><thead><tr><th scope="col">Name</th><th scope="col">Kind</th><th scope="col">Source</th><th scope="col">Children</th></tr></thead><tbody>${rows}</tbody></table>${p.next_cursor ? `<button type="button" data-more="${esc(p.next_cursor)}">Load more</button>` : ''}`;
  }
  function detail() {
    const d = st.detail; if (!d) return '';
    const n = d.node, url = sourceUrl(snap(), n);
    const ev = d.edges?.edges || [];
    return `<aside class="arch-detail" aria-label="Selected item"><h3>${esc(n.qualified_name)}</h3><dl><dt>Kind</dt><dd>${esc(n.kind)}</dd><dt>Source</dt><dd>${url ? `<a href="${esc(url)}" rel="noopener" target="_blank">${esc(n.relative_path)}:${n.line_start}</a>` : esc((n.relative_path || '—') + (n.line_start ? ':' + n.line_start : ''))}</dd><dt>Extractor</dt><dd>${esc(n.extractor)} · confidence ${esc(String(n.confidence))}</dd></dl>
      <h4>Dependencies${d.edges?.incomplete ? ' (first page)' : ''}</h4>${ev.length ? `<ul class="arch-edges">${ev.map(e => `<li data-edge="${esc(e.edge_id)}">${esc(e.kind)} ${esc(e.from_id === n.node_id ? '→ ' + e.to_id : '← ' + e.from_id)} <small>${esc(e.source_span.path)}:${e.source_span.line_start} · ${e.inferred ? 'inferred' : 'static'} · ${esc(e.extractor)}</small></li>`).join('')}</ul>` : '<p class="arch-note">No static dependency edges recorded.</p>'}</aside>`;
  }
  function diffPanel() {
    const s = snap(), others = st.snapshots.filter(x => x.repository_id === s.repository_id && x.snapshot_id !== s.snapshot_id);
    if (!others.length) return '';
    const d = st.diff;
    return `<section class="arch-diff" aria-label="Compare snapshots"><label>Compare with base <select data-diff>${'<option value="">Choose a snapshot</option>'}${others.map(o => `<option value="${esc(o.snapshot_id)}"${d?.base === o.snapshot_id ? ' selected' : ''}>${esc(label(o))}</option>`).join('')}</select></label>${d ? `<p data-testid="arch-diff-summary">Nodes +${d.summary.nodes_added} −${d.summary.nodes_removed} ~${d.summary.nodes_modified} · edges +${d.summary.edges_added} −${d.summary.edges_removed}</p><ul class="arch-diff-list">${d.items.slice(0, 50).map(i => `<li class="chg-${esc(i.change)}">${esc(i.change)} ${esc(i.type)} ${esc(i.id || `${i.kind} ${i.from_id} → ${i.to_id}`)}</li>`).join('')}</ul>${d.warnings.map(w => `<p class="arch-note">${esc(w)}</p>`).join('')}` : ''}</section>`;
  }
  function render() {
    if (!alive) return;
    const s = snap();
    if (!st.snapshots.length) {
      const a = st.analyses[0];
      host.innerHTML = `<h1>Architecture</h1><herdr-empty message="No architecture snapshots yet. Run: herdr-eng architecture analyze --repo . --url &lt;control plane&gt;"></herdr-empty>${a ? `<p class="arch-note">Latest analysis ${esc(a.analysis_id)}: ${esc(a.state)}${a.message ? ' — ' + esc(a.message) : ''}</p>` : ''}`;
      return;
    }
    const notices = snapshotNotices(s, st.snapshots).map(n => `<p class="arch-notice ${n.kind}" role="status" data-notice="${n.kind}">${esc(n.text)}</p>`).join('');
    const diags = (st.page && s ? '' : '');
    const failed = st.analyses.filter(a => a.state === 'failed').slice(0, 3).map(a => `<p class="arch-notice failed" role="alert">Analysis ${esc(a.analysis_id)} failed${a.message ? ': ' + esc(a.message) : ''}</p>`).join('');
    const running = st.analyses.filter(a => ['started', 'progress'].includes(a.state)).map(a => `<p class="arch-notice running" role="status">Analysis ${esc(a.analysis_id)} ${esc(a.state)}${typeof a.progress === 'number' ? ` ${Math.round(a.progress * 100)}%` : ''}</p>`).join('');
    host.innerHTML = `<h1>Architecture</h1>
      <div class="arch-bar"><label>Snapshot <select data-snap>${st.snapshots.map(x => `<option value="${esc(x.snapshot_id)}"${x.snapshot_id === st.sid ? ' selected' : ''}>${esc(label(x))}</option>`).join('')}</select></label>
      <label>Search symbols <input type="search" data-q value="${esc(st.q)}" placeholder="name or path"></label></div>
      <p class="arch-meta">Commit <code>${esc(s.commit_sha.slice(0, 12))}</code> · ${esc(s.completeness)} · ${s.counts.nodes} nodes, ${s.counts.edges} edges · host ${esc(s.host_id)} · analyzer ${esc(s.analyzer_version)}</p>
      ${notices}${failed}${running}${st.q ? '' : crumbs()}
      <div class="arch-main"><div class="arch-left">${graphSvg()}${table()}</div>${detail()}</div>${diffPanel()}`;
  }
  async function open(id) {
    try {
      const [n, e] = await Promise.all([client.getArchitectureNode(st.sid, id, {signal: ctl.signal}),
        client.expandArchitectureNode(st.sid, id, {limit: 50, signal: ctl.signal})]);
      st.detail = {...n, edges: e};
      if (n.node.child_count) { st.parent = id; st.q = ''; await browse(); } else render();
    } catch (err) { if (err.name !== 'AbortError') fail(err); }
  }
  host.addEventListener('click', ev => {
    const t = ev.target.closest('[data-node],[data-open],[data-crumb],[data-more]');
    if (!t) return;
    if (t.dataset.more) browse(t.dataset.more);
    else if (t.dataset.crumb !== undefined) { st.parent = t.dataset.crumb; st.q = ''; st.detail = null; browse(); }
    else open(t.dataset.node || t.dataset.open);
  });
  host.addEventListener('keydown', ev => {
    const g = ev.target.closest?.('[data-node]');
    if (g && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); open(g.dataset.node); }
  });
  host.addEventListener('change', async ev => {
    if (ev.target.matches('[data-snap]')) { st.sid = ev.target.value; st.parent = ''; st.q = ''; st.detail = null; st.diff = null; await browse(); }
    if (ev.target.matches('[data-diff]')) {
      st.diff = null;
      if (ev.target.value) try { st.diff = await client.diffArchitecture(ev.target.value, st.sid, {signal: ctl.signal}); } catch (e) { return fail(e); }
      render();
    }
  });
  let timer = null;
  host.addEventListener('input', ev => {
    if (!ev.target.matches('[data-q]')) return;
    clearTimeout(timer);
    const v = ev.target.value.trim();
    timer = setTimeout(async () => { st.q = v; await browse(); host.querySelector('[data-q]')?.focus(); const i = host.querySelector('[data-q]'); i?.setSelectionRange(v.length, v.length); }, 250);
  });
  load();
  return {dispose() { alive = false; ctl.abort(); clearTimeout(timer); }};
}
