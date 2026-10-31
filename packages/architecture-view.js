// Architecture tab: browse immutable commit-keyed code-graph snapshots. A client of
// /api/v1/architecture/*; renders loading / disabled / empty / error / stale / partial states.
import {escapeHtml as esc} from './herdr-web-components/util.js';
import {layoutGraph, NODE_W, NODE_H} from './architecture-layout.js';
import {skeleton} from './motion.js';

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
  if (snapshot.completeness !== 'complete') out.push({kind: 'partial', text: 'Partial snapshot: some languages are not analysed, or a limit was reached.'});
  const newer = all.find(s => s.repository_id === snapshot.repository_id && s.snapshot_id !== snapshot.snapshot_id && s.created_at > snapshot.created_at);
  if (newer) out.push({kind: 'stale', text: `Stale: a newer snapshot exists (${newer.commit_sha.slice(0, 8)}).`});
  return out;
}
// Newest snapshot of every OTHER repository plus the selected one (bounded), for cross-repository resolution.
export function crossTargets(snapshots, selected, max = 8) {
  const newest = new Map();
  for (const s of snapshots) {
    if (s.repository_id === selected?.repository_id) continue;
    const cur = newest.get(s.repository_id);
    if (!cur || s.created_at > cur.created_at) newest.set(s.repository_id, s);
  }
  return selected ? [selected, ...[...newest.values()].sort((a, b) => a.repository_id.localeCompare(b.repository_id))].slice(0, max) : [];
}
// Unfinished analysis jobs (requested / claimed and running) and their Cancel controls. The view cannot know the
// key's role, so the button is always offered; a non-operator key gets the server's 401/403 refusal, shown escaped.
const OPEN_STATES = new Set(['requested', 'started', 'progress']);
export function cancellableJobs(analyses = []) {
  return analyses.filter(a => a && OPEN_STATES.has(a.state) && typeof a.analysis_id === 'string');
}
export function jobsPanel(analyses = [], cancels = {}) {
  const jobs = cancellableJobs(analyses);
  if (!jobs.length) return '';
  const row = a => {
    const c = cancels[a.analysis_id] || {};
    const what = [a.repository_id && text(a.repository_id).replace(/^github\.com\//, ''), a.rev && `@ ${text(a.rev)}`, a.host_id && `on ${text(a.host_id)}`].filter(Boolean).join(' ');
    return `<li data-job="${esc(a.analysis_id)}">${esc(a.analysis_id)} · ${esc(a.state)}${what ? ` · ${esc(what)}` : ''}
      <button type="button" data-cancel="${esc(a.analysis_id)}" aria-label="Cancel analysis ${esc(a.analysis_id)}"${c.pending ? ' disabled aria-busy="true"' : ''}>${c.pending ? 'Cancelling…' : 'Cancel'}</button>
      ${c.error ? `<span class="arch-notice failed" role="alert" data-cancel-error>${esc(c.error)}</span>` : ''}</li>`;
  };
  return `<section class="arch-jobs" aria-label="Analysis jobs" data-testid="arch-jobs"><h2>Analysis jobs</h2>
    <p class="arch-note">Cancelling needs an operator key; a running host stops at its next heartbeat.</p><ul>${jobs.map(row).join('')}</ul></section>`;
}
// Ask, then cancel. Resolves {cancelled: false} when the operator declines, {analysis} on success, {error} otherwise.
export async function cancelJob(client, analysisId, {confirm = globalThis.confirm, signal} = {}) {
  if (!(typeof confirm === 'function' && confirm(`Cancel analysis ${analysisId}?`))) return {cancelled: false};
  try {
    const out = await client.cancelArchitectureAnalysis(analysisId, {signal});
    return {cancelled: true, analysis: out.analysis};
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    const why = e.status === 401 || e.status === 403 ? 'Only an operator key can cancel analyses' : 'Cancel failed';
    return {cancelled: false, error: `${why}: ${e.message || `HTTP ${e.status}`}`};
  }
}
const label = s => `${text(s.repository_id).replace(/^github\.com\//, '')} @ ${text(s.commit_sha).slice(0, 8)} · ${text(s.host_id)} · wt ${text(s.worktree_id).slice(0, 6)}`;

export function mountArchitecture(root, client, opts = {}) {
  let alive = true, ctl = new AbortController();
  const st = {edgeKind: 'imports', snapshots: [], sid: opts.snapshot || '', parent: opts.parent || '', q: opts.q || '', page: null, detail: null, diff: null,
    analyses: [], cursor: null, caps: [], extras: null, timeline: null, cross: null, explain: null, maps: [], request: null, corr: null, rooms: null, cancels: {}};
  root.innerHTML = `<div class="arch">${skeleton('architecture')}</div>`;
  const host = root.firstElementChild;
  const snap = () => st.snapshots.find(s => s.snapshot_id === st.sid);
  const fail = e => { if (!alive) return; opts.onFreshness?.(e.status === 404, e.message); host.innerHTML = e.status === 404
    ? '<herdr-empty message="The Architecture Explorer is not enabled on this control plane (control_plane.architecture.enabled)."></herdr-empty>'
    : `<herdr-error message="${esc(e.message || 'request failed')}"></herdr-error>`; };

  async function load() {
    try {
      const [s, a] = await Promise.all([client.listArchitectureSnapshots({limit: 200, signal: ctl.signal}),
        client.listArchitectureAnalyses({signal: ctl.signal}).catch(() => ({analyses: []}))]);
      if (!alive) return;
      st.snapshots = s.snapshots; st.analyses = a.analyses;
      st.caps = (await client.health({signal: ctl.signal}).catch(() => ({}))).capabilities || [];
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
      loadExtras();
    } catch (e) { if (e.name !== 'AbortError') fail(e); }
  }
  async function loadExtras() {
    const s = snap(), key = s?.snapshot_id;
    if (!s || st.extras?.id === key) return;
    const [o, t, b] = await Promise.all([client.getArchitectureOverlays(key, {signal: ctl.signal}).catch(() => null),
      client.getArchitectureTimeline(s.repository_id, {signal: ctl.signal}).catch(() => null),
      client.getArchitectureBase(key, {signal: ctl.signal}).catch(() => null)]);
    if (!alive || snap()?.snapshot_id !== key) return;
    st.extras = {id: key, overlays: o, base: b}; st.timeline = t; render();
  }
  function graphSvg() {
    const p = st.page;
    if (!p || st.q || !p.nodes.length) return '';
    const shown = p.edges.filter(e => st.edgeKind === 'all' || e.kind === st.edgeKind);
    const L = layoutGraph(p.nodes.map(n => ({id: n.node_id, n})), shown.map(e => ({from: e.from_id, to: e.to_id, count: e.count, e})));
    const noun = {imports: 'import', calls: 'call'};
    const edges = L.edges.map(e => `<path class="arch-edge ${esc(e.e.kind)}${e.back ? ' back' : ''}" d="${e.d}" marker-end="url(#arch-arrow)"><title>${esc(`${e.from} → ${e.to} (${e.count} ${noun[e.e.kind] || e.e.kind}${e.count === 1 ? '' : 's'}${e.e.inferred ? ', inferred' : ''})`)}</title></path>`).join('');
    const kinds = `<label class="arch-edgekind">Edges <select data-edgekind>${[['imports', 'Imports'], ['calls', 'Calls'], ['all', 'Imports and calls']].map(([v, t]) => `<option value="${v}"${st.edgeKind === v ? ' selected' : ''}>${t}</option>`).join('')}</select></label>`;
    const nodes = L.nodes.map(v => `<g class="arch-node kind-${esc(v.n.kind)}" transform="translate(${v.x},${v.y})" tabindex="0" role="button" data-node="${esc(v.id)}" aria-label="${esc(`${v.n.kind} ${v.n.qualified_name}${v.n.child_count ? ', ' + v.n.child_count + ' children, press Enter to open' : ''}`)}"><rect width="${NODE_W}" height="${NODE_H}" rx="6"/><text x="10" y="19" class="t1">${esc(v.n.qualified_name.split('.').pop().slice(0, 24))}</text><text x="10" y="35" class="t2">${esc(v.n.kind)}${v.n.child_count ? ` · ${v.n.child_count}` : ''}</text></g>`).join('');
    return `${kinds}<div class="arch-graph" data-testid="arch-graph"><svg viewBox="0 0 ${L.width} ${L.height}" width="${L.width}" height="${L.height}" role="group" aria-label="Dependency graph of the ${p.nodes.length} items below; the table lists the same items"><defs><marker id="arch-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L8,4 L0,8 z"/></marker></defs>${edges}${nodes}</svg></div>${L.hidden ? `<p class="arch-note">Showing the ${L.nodes.length} best-connected of ${p.nodes.length} items in the graph; the table has all of them.</p>` : ''}`;
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
  function corrBlock() {
    const c = st.corr, r = st.rooms, x = st.explain;
    const explainBtn = st.caps.includes('architecture_explain_v1') ? `<p><button type="button" data-explain>Explain (AI, from derived facts only)</button></p>` : '';
    const xr = x ? (x.pending ? '<p class="arch-notice running" role="status">Asking the configured route…</p>' : x.error ? `<p class="arch-notice failed" role="alert">${esc(x.error)}</p>` : `<div class="arch-explain" data-testid="arch-explain"><p><strong>Inferred explanation</strong> · model ${esc(x.model)} · source code not sent</p><p>${esc(x.explanation)}</p><p class="arch-note">Cited edges: ${x.cited_edges.length ? x.cited_edges.map(esc).join(', ') : 'none'}${x.dropped_citations.length ? ` · ${x.dropped_citations.length} unverifiable citation(s) removed` : ''}</p></div>`) : '';
    return `<h4>Agents that changed this</h4>${!c ? '<p class="arch-note">Loading…</p>' : c.correlations.length ? `<ul data-corr>${c.correlations.map(k => `<li>${esc(k.session_id)} · ${esc(k.flow)} · ${esc(k.change || '')}${k.parent_worker_id ? ' · parent ' + esc(k.parent_worker_id) : ''}${k.pr_ref ? ' · ' + esc(k.pr_ref) : ''}</li>`).join('')}</ul>` : `<p class="arch-note">${c.change === 'unknown' ? 'No earlier snapshot to compare with, so changes cannot be attributed.' : c.change === 'unchanged' ? 'Not changed by this commit.' : 'No agent recorded at this commit.'}</p>`}${c?.notes?.length ? c.notes.map(n => `<p class="arch-note">${esc(n)}</p>`).join('') : ''}
      <p class="arch-note" data-rooms>${r ? `Discussed in ${r.count ?? (r.rooms || []).length} room(s).` : ''}</p>${explainBtn}${xr}`;
  }
  function detail() {
    const d = st.detail; if (!d) return '';
    const n = d.node, url = sourceUrl(snap(), n);
    const ev = d.edges?.edges || [];
    return `<aside class="arch-detail" aria-label="Selected item"><h3>${esc(n.qualified_name)}</h3><dl><dt>Kind</dt><dd>${esc(n.kind)}</dd><dt>Source</dt><dd>${url ? `<a href="${esc(url)}" rel="noopener" target="_blank">${esc(n.relative_path)}:${n.line_start}</a>` : esc((n.relative_path || '—') + (n.line_start ? ':' + n.line_start : ''))}</dd><dt>Extractor</dt><dd>${esc(n.extractor)} · confidence ${esc(String(n.confidence))}</dd></dl>
      ${corrBlock()}
      <h4>Dependencies${d.edges?.incomplete ? ' (first page)' : ''}</h4>${ev.length ? `<ul class="arch-edges">${ev.map(e => `<li data-edge="${esc(e.edge_id)}">${esc(e.kind)} ${esc(e.from_id === n.node_id ? '→ ' + e.to_id : '← ' + e.from_id)} <small>${esc(e.source_span.path)}:${e.source_span.line_start} · ${e.inferred ? 'inferred' : 'static'} · ${esc(e.extractor)}</small></li>`).join('')}</ul>` : '<p class="arch-note">No static dependency edges recorded.</p>'}</aside>`;
  }
  function diffPanel() {
    const s = snap(), others = st.snapshots.filter(x => x.repository_id === s.repository_id && x.snapshot_id !== s.snapshot_id);
    if (!others.length) return '';
    const d = st.diff, pb = st.extras?.base?.base;
    const prBase = pb ? `<p><button type="button" data-pr-base="${esc(pb.snapshot_id)}">Compare with PR base</button> <small>merge-base ${esc(String(pb.commit_sha).slice(0, 8))} of ${esc(pb.base_rev)}, resolved by the analysis host's git history</small></p>`
      : (st.extras?.base?.reason && !/no analysis asked/.test(st.extras.base.reason) ? `<p class="arch-note" data-pr-base-reason>No PR base: ${esc(st.extras.base.reason)}</p>` : '');
    return `<section class="arch-diff" aria-label="Compare snapshots">${prBase}<label>Compare with base <select data-diff>${'<option value="">Choose a snapshot</option>'}${others.map(o => `<option value="${esc(o.snapshot_id)}"${d?.base === o.snapshot_id ? ' selected' : ''}>${esc(label(o))}</option>`).join('')}</select></label>${d ? `<p data-testid="arch-diff-summary">Nodes +${d.summary.nodes_added} −${d.summary.nodes_removed} ~${d.summary.nodes_modified} · edges +${d.summary.edges_added} −${d.summary.edges_removed}</p><ul class="arch-diff-list">${d.items.slice(0, 50).map(i => `<li class="chg-${esc(i.change)}">${esc(i.change)} ${esc(i.type)} ${esc(i.id || `${i.kind} ${i.from_id} → ${i.to_id}`)}</li>`).join('')}</ul>${d.warnings.map(w => `<p class="arch-note">${esc(w)}</p>`).join('')}` : ''}</section>`;
  }
  const list = (items, empty, fmt) => items?.length ? `<ul>${items.map(fmt).join('')}</ul>` : `<p class="arch-note">${empty}</p>`;
  function activityPanel() {
    const o = st.extras?.overlays;
    if (!o) return '<section class="arch-activity" aria-label="Related activity"><h2>Related activity</h2><p class="arch-note">Related records are unavailable.</p></section>';
    const link = (u, t) => u && /^https:\/\//.test(u) ? `<a href="${esc(u)}" rel="noopener" target="_blank">${esc(t)}</a>` : esc(t);
    return `<section class="arch-activity" aria-label="Related activity" data-testid="arch-activity"><h2>Related activity</h2>
      <p class="arch-note">Linked by exact repository and full commit only; branch names and working directories are not evidence.</p>
      <h3>CI runs</h3>${list(o.ci, 'No CI run recorded for this commit.', r => `<li data-ci>${link(r.url, `#${r.run_number ?? '?'}`)} · ${esc(r.state || r.status || 'unknown')}${r.branch ? ' · ' + esc(r.branch) : ''}</li>`)}
      <h3>Pull requests</h3>${list(o.pull_requests, 'No pull request has this commit as its head.', p => `<li data-pr>${link(p.url, p.pr_ref)} · ${esc(p.state)}</li>`)}
      <h3>Agents</h3>${list(o.sessions, 'No agent session recorded at this commit.', x => `<li data-session>${esc(x.session_id)} · ${esc(x.agent_role || 'agent')} · ${esc(x.flow)}${x.parent_session_id ? ' · parent ' + esc(x.parent_session_id) : ''}</li>`)}
      <h3>Same worktree, other commits <small>(context, not authorship)</small></h3>${list(o.worktree_sessions, 'No other agent recorded in this worktree.', x => `<li data-wt-session>${esc(x.session_id)} · ${esc(x.agent_role || 'agent')} · ${x.commit_sha ? esc(x.commit_sha.slice(0, 8)) : 'no commit recorded'}</li>`)}
      <h3>Missions</h3>${list(o.missions, 'No mission linked.', m => `<li data-mission>${esc(m.mission_key || m.mission_id)}</li>`)}
      <h3>Issue mentions <small>(inferred from mission text)</small></h3>${list(o.issues, 'No explicit issue reference.', i => `<li data-issue>${esc(i.issue_ref)} <small>inferred · ${esc(i.evidence)}</small></li>`)}</section>`;
  }
  function timelinePanel() {
    const t = st.timeline;
    if (!t) return '';
    return `<section class="arch-timeline" aria-label="Timeline" data-testid="arch-timeline"><h2>Timeline</h2><p class="arch-note">${esc(t.note)}</p>
      <div class="arch-lanes"><div><h3>Architecture snapshots</h3>${list(t.architecture, 'No snapshots.', x => `<li data-tl-snap="${esc(x.snapshot_id)}"><button type="button" class="link" data-pick="${esc(x.snapshot_id)}">${esc(x.commit_sha.slice(0, 8))}</button> <small>${esc(x.at)} · ${esc(x.completeness)}${x.session_ids.length ? ` · ${x.session_ids.length} agent(s) at this commit` : ''}</small></li>`)}</div>
      <div><h3>Agent spawns</h3>${list(t.agents, 'No agent sessions recorded for this repository.', x => `<li data-tl-agent="${esc(x.session_id)}">${esc(x.session_id)} <small>${esc(x.at || '')}${x.parent_session_id ? ' · parent ' + esc(x.parent_session_id) : ''}${x.commit_sha ? ' · ' + esc(x.commit_sha.slice(0, 8)) : ' · no exact commit (unlinked)'}</small>${x.snapshot_ids.map(id => ` <button type="button" class="link" data-pick="${esc(id)}">snapshot</button>`).join('')}</li>`)}</div></div></section>`;
  }
  function requestPanel() {
    const s = snap(), r = st.request;
    return `<section class="arch-request" aria-label="Request analysis"><h2>Request analysis</h2>
      <form data-request><label>Repository <input name="repository_id" value="${esc(draft("repository_id", s.repository_id))}" required></label>
      <label>Host <input name="host_id" value="${esc(draft("host_id", s.host_id))}" required></label>
      <label>Revision <input name="rev" value="${esc(draft("rev", "HEAD"))}" required></label>
      <label>PR base ref <input name="base_rev" value="${esc(draft("base_rev", ""))}" placeholder="optional, e.g. origin/main"></label><button type="submit">Request analysis</button></form>
      ${r ? `<p class="arch-notice ${r.error ? 'failed' : 'running'}" role="status" data-request-result>${esc(r.error ? r.error : r.deduplicated ? (r.snapshot_id ? 'A snapshot for that commit already exists.' : `Already queued (${r.analysis_id}).`) : `Queued ${r.analysis_id}. A host running "herdr-eng architecture serve" will pick it up.`)}</p>` : ''}</section>`;
  }
  function crossPanel() {
    const targets = crossTargets(st.snapshots, snap());
    if (targets.length < 2) return '';
    const c = st.cross, repos = targets.map(x => x.repository_id);
    const row = e => `<li data-xedge>${esc(e.from.repository_id)} ${esc(e.from.node_id)} → ${esc(e.to.repository_id)} ${esc(e.to.node_id)} <small>${esc(e.source_span.path)}:${e.source_span.line_start} · ${esc(e.resolution)}</small></li>`;
    return `<section class="arch-cross" aria-label="Cross-repository dependencies"><h2>Cross-repository</h2>
      <form data-mapform aria-label="Add a package mapping"><label>Importer <select name="importer">${repos.map(r => `<option>${esc(r)}</option>`).join('')}</select></label>
        <label>Package <input name="package" value="${esc(draft("package", ""))}" required pattern="[^|]+"></label>
        <label>Provided by <select name="provider">${repos.map(r => `<option>${esc(r)}</option>`).join('')}</select></label><button type="submit">Add mapping</button></form>
      ${st.maps.length ? `<ul data-maps>${st.maps.map((m, i) => `<li>${esc(m.importer)}: <code>${esc(m.package)}</code> → ${esc(m.provider)} <button type="button" class="link" data-unmap="${i}">remove</button></li>`).join('')}</ul>` : ''}
      <button type="button" data-cross>Resolve across ${targets.length} repositories</button>
      ${c ? (c.error ? `<p class="arch-notice failed" role="alert">${esc(c.error)}</p>` : `<p data-testid="arch-cross-summary">${c.verified.length} verified · ${c.inferred.length} inferred · ${c.ambiguous.length} ambiguous${c.incomplete ? ' · incomplete (limit reached)' : ''}</p>
      <h3>Verified (explicit mapping)</h3>${list(c.verified.slice(0, 30), 'None. Add a mapping above to state which repository provides a package; only then is an edge verified.', row)}
      <h3>Inferred (name match only)</h3>${list(c.inferred.slice(0, 30), 'None.', row)}
      ${c.warnings.map(w => `<p class="arch-note">${esc(w)}</p>`).join('')}`) : ''}</section>`;
  }
  // The view repaints its whole HTML when a background load finishes; keep what the user is typing.
  st.draft = {}; st.focus = null;
  const draft = (name, dflt) => st.draft[name] ?? dflt;
  host.addEventListener('input', ev => { if (ev.target.name && ev.target.closest('[data-request],[data-mapform]')) st.draft[ev.target.name] = ev.target.value; });
  host.addEventListener('focusin', ev => { st.focus = ev.target.closest?.('[data-request],[data-mapform]') ? ev.target.name || null : null; });
  function restoreFocus() {
    if (!st.focus) return;
    const el = host.querySelector(`[data-request] [name="${st.focus}"], [data-mapform] [name="${st.focus}"]`);
    if (el && document.activeElement !== el) { el.focus(); try { el.setSelectionRange(el.value.length, el.value.length); } catch { /* select: no range */ } }
  }
  function render() {
    if (!alive) return;
    opts.onFreshness?.(true);
    const s = snap();
    if (!st.snapshots.length) {
      const a = st.analyses[0];
      host.innerHTML = `<h1>Architecture</h1><herdr-empty message="No architecture snapshots yet. Run: herdr-eng architecture analyze --repo . --url &lt;control plane&gt;"></herdr-empty>${a ? `<p class="arch-note">Latest analysis ${esc(a.analysis_id)}: ${esc(a.state)}${a.message ? ' — ' + esc(a.message) : ''}</p>` : ''}${jobsPanel(st.analyses, st.cancels)}`;
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
      <div class="arch-main"><div class="arch-left">${graphSvg()}${table()}</div>${detail()}</div>${diffPanel()}${timelinePanel()}${activityPanel()}${crossPanel()}${jobsPanel(st.analyses, st.cancels)}${requestPanel()}`;
    restoreFocus();
  }
  async function open(id) {
    try {
      const [n, e] = await Promise.all([client.getArchitectureNode(st.sid, id, {signal: ctl.signal}),
        client.expandArchitectureNode(st.sid, id, {limit: 50, signal: ctl.signal})]);
      st.detail = {...n, edges: e}; st.corr = null; st.rooms = null; st.explain = null;
      client.getArchitectureCorrelations(st.sid, {node: id, signal: ctl.signal}).then(c => { st.corr = c; }, () => { st.corr = {correlations: [], notes: [], change: 'unknown'}; })
        .then(() => client.getArchitectureNodeRooms(st.sid, id, {signal: ctl.signal}).then(r => { st.rooms = r; }, () => {})).then(() => alive && render());
      if (n.node.child_count) { st.parent = id; st.q = ''; await browse(); } else render();
    } catch (err) { if (err.name !== 'AbortError') fail(err); }
  }
  async function cancelClick(id) {
    if (st.cancels[id]?.pending) return;
    st.cancels[id] = {pending: true}; render();
    let out;
    try { out = await cancelJob(client, id, {signal: ctl.signal, confirm: opts.confirm}); } catch (e) { if (e.name === 'AbortError') return; out = {error: e.message}; }
    if (!alive) return;
    st.cancels[id] = out.error ? {error: out.error} : {};
    render();
    if (out.cancelled) client.listArchitectureAnalyses({signal: ctl.signal}).then(a => { st.analyses = a.analyses; render(); }, () => {});
  }
  host.addEventListener('click', ev => {
    const cx = ev.target.closest('[data-cancel]');
    if (cx) return cancelClick(cx.dataset.cancel);
    const ex = ev.target.closest('[data-explain],[data-cross],[data-pick],[data-unmap],[data-pr-base]');
    if (ex) return extraClick(ex);
    const t = ev.target.closest('[data-node],[data-open],[data-crumb],[data-more]');
    if (!t) return;
    if (t.dataset.more) browse(t.dataset.more);
    else if (t.dataset.crumb !== undefined) { st.parent = t.dataset.crumb; st.q = ''; st.detail = null; browse(); }
    else open(t.dataset.node || t.dataset.open);
  });
  async function extraClick(t) {
    try {
      if (t.dataset.unmap !== undefined) { st.maps.splice(Number(t.dataset.unmap), 1); render(); }
      else if (t.dataset.prBase) { st.diff = await client.diffArchitecture(t.dataset.prBase, st.sid, {signal: ctl.signal}); render(); }
      else if (t.dataset.pick) { st.sid = t.dataset.pick; st.parent = ''; st.q = ''; st.detail = null; st.diff = null; st.extras = null; await browse(); }
      else if (t.dataset.cross !== undefined) {
        const ids = crossTargets(st.snapshots, snap()).map(x => x.snapshot_id);
        st.cross = await client.getArchitectureCrossRepo(ids, st.maps, {signal: ctl.signal}).catch(e => ({error: e.message})); render();
      } else if (t.dataset.explain !== undefined && st.detail) {
        st.explain = {pending: true}; render();
        st.explain = await client.explainArchitectureNode(st.sid, st.detail.node.node_id, {signal: ctl.signal}).catch(e => ({error: e.message})); render();
      }
    } catch (e) { if (e.name !== 'AbortError') fail(e); }
  }
  host.addEventListener('submit', ev => {
    if (!ev.target.matches('[data-mapform]')) return;
    ev.preventDefault();
    const f = new FormData(ev.target), m = {importer: f.get('importer'), package: String(f.get('package')).trim(), provider: f.get('provider')};
    if (m.package && !m.package.includes('|') && !st.maps.some(x => x.importer === m.importer && x.package === m.package)) st.maps.push(m);
    render();
  });
  host.addEventListener('submit', async ev => {
    if (!ev.target.matches('[data-request]')) return;
    ev.preventDefault();
    const f = new FormData(ev.target), br = String(f.get('base_rev') || '').trim();
    if (br && !/^[A-Za-z0-9][A-Za-z0-9._\/-]{0,199}$/.test(br)) { st.request = {error: 'PR base ref must be a branch, tag or commit name'}; render(); return; }
    st.request = await client.requestArchitectureAnalysis({repository_id: f.get('repository_id'), host_id: f.get('host_id'), rev: f.get('rev'), ...(br ? {base_rev: br} : {})}, {signal: ctl.signal}).catch(e => ({error: e.message}));
    render();
    client.listArchitectureAnalyses({signal: ctl.signal}).then(a => { st.analyses = a.analyses; render(); }, () => {});
  });
  host.addEventListener('keydown', ev => {
    const g = ev.target.closest?.('[data-node]');
    if (g && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); open(g.dataset.node); }
  });
  host.addEventListener('change', async ev => {
    if (ev.target.matches('[data-snap]')) { st.sid = ev.target.value; st.parent = ''; st.q = ''; st.detail = null; st.diff = null; await browse(); }
    if (ev.target.matches('[data-edgekind]')) { st.edgeKind = ev.target.value; render(); }
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
