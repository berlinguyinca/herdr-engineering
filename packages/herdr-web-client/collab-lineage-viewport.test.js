// The Mission Workbench lineage graph as a viewport, and the inspector's measured facts (S5).
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  COL_W, FIT_FLOOR, MAX_DRAWN_EDGES, MAX_DRAWN_NODES, NOT_REPORTED, ROW_H, ZOOM_MAX, ZOOM_MIN, centerOn, clampView, cullGraph, edgeLabel, filterGraph, fitView,
  focusSubgraph, focusKey, focusSelector, inspectorFacts, isVisible, layoutCached, layoutGraph, mergePages, nextNode, panBy, renderGanttSvg, buildGantt, renderGraphSvg,
  renderGraphToolbar, renderInspector, renderLineage, renderModeBadge, renderReplay, renderStatusStrip, statusSummary, viewportSize, visibleRect, zoomAt,
} from '../collab-lineage.js';

const node = (id, extra = {}) => ({ id, node_type: id.split(':')[0], label: extra.label ?? id.split(':').slice(1).join(':'), state: extra.state ?? null, attrs: extra.attrs || {}, visibility_scope: 'room', ...extra });
const edge = (t, a, b, extra = {}) => ({ edge_type: t, from_id: a, to_id: b, attrs: {}, stale: false, removed: false, ...extra });
const HOSTILE = '<img src=x onerror=alert(1)>"onmouseover="x\'';
const count = (html, re) => [...html.matchAll(re)].length;

// a chain of n sessions with a handoff record between each pair, a checkpoint artifact produced by the attempt
function chain(n) {
  const nodes = [node('agent:lead', { state: 'running', label: 'Lead' }), node('attempt:a1', { state: 'running', label: 'Attempt' })];
  const edges = [edge('spawn', 'agent:lead', 'attempt:a1')];
  for (let i = 1; i <= n; i += 1) {
    nodes.push(node(`session:s${i}`, { state: i === n ? 'running' : 'done', label: `Session ${i}`, attrs: { attempt_id: 'a1' } }));
    edges.push(edge('spawn', 'attempt:a1', `session:s${i}`));
    if (i > 1) {
      nodes.push(node(`handoff:h${i}`, { state: 'committed', attrs: { from_session_id: `session:s${i - 1}`, to_session_id: `session:s${i}`, checkpoint_sha: `${i}`.padStart(40, 'c'), handoff_kind: 'context_rollover' } }));
      edges.push(edge('handoff', `session:s${i - 1}`, `session:s${i}`, { attrs: { handoff_id: `handoff:h${i}`, handoff_kind: 'context_rollover', cause: 'session_rollover' } }));
    }
  }
  return { nodes, edges };
}

test('fit never magnifies past 1:1 and never shrinks a wide graph below the readable floor', () => {
  const small = layoutGraph({ nodes: [node('task:a'), node('task:b')], edges: [edge('dependency', 'task:a', 'task:b')] });
  assert.deepEqual(fitView(small, 960, 480), { x: 0, y: 0, k: 1 });
  const wide = layoutGraph(chain(60));
  const v = fitView(wide, 960, 480);
  assert.equal(v.k, FIT_FLOOR, 'a 60-session chain is shown from its start at the floor, not as specks');
  assert.ok(wide.width > 60 * COL_W);
});

test('zoom keeps the point under the pointer still, clamps, and pan moves by screen pixels', () => {
  const v0 = { x: 100, y: 50, k: 1 };
  const v1 = zoomAt(v0, 2, 300, 200);
  assert.equal(v1.k, 2);
  assert.equal(v1.x + 300 / v1.k, v0.x + 300 / v0.k);
  assert.equal(v1.y + 200 / v1.k, v0.y + 200 / v0.k);
  assert.equal(zoomAt(v0, 1e6, 0, 0).k, ZOOM_MAX);
  assert.equal(zoomAt(v0, 1e-6, 0, 0).k, ZOOM_MIN);
  assert.deepEqual(panBy({ x: 0, y: 0, k: 2 }, 40, -20), { x: 20, y: -10, k: 2 });
  const layout = layoutGraph(chain(5));
  const c = clampView({ x: 1e9, y: -1e9, k: 1 }, layout, 960, 480);
  assert.ok(c.x < layout.width && c.y > -480, 'a view cannot be dragged entirely off the graph');
  assert.ok(Number.isFinite(clampView({ x: NaN, y: 0, k: NaN }, layout, 960, 480).k));
});

test('culling returns exactly the nodes that meet the rectangle, found by column and row', () => {
  const g = chain(40);
  const layout = layoutGraph(g);
  const rect = { x0: 5 * COL_W, y0: 0, x1: 8 * COL_W, y1: 3 * ROW_H };
  const cull = cullGraph(g, layout, rect, {});
  const drawn = new Set(cull.nodes.map((p) => p.node.id));
  for (const p of layout.placed.values()) {
    const meets = p.x + p.w >= rect.x0 && p.x <= rect.x1 && p.y + p.h >= rect.y0 && p.y <= rect.y1;
    if (meets) assert.ok(drawn.has(p.node.id), `${p.node.id} meets the rectangle and is drawn`);
  }
  assert.ok(cull.nodes.length < layout.placed.size / 2, 'most of the graph is left out');
  for (const e of cull.edges) assert.ok(drawn.has(e.from_id) || drawn.has(e.to_id), 'an edge is drawn only at a drawn node');
});

test('the selected and keyboard-focused nodes are drawn even when out of view, so the single tab stop and the selection survive', () => {
  const g = chain(40);
  const layout = layoutGraph(g);
  const html = renderGraphSvg(g, layout, { sel: 'session:s40', focus: 'session:s30', view: { x: 0, y: 0, k: 1 }, vw: 960, vh: 480 });
  assert.match(html, /data-node="session:s40"/);
  assert.match(html, /data-node="session:s30"[^>]*/);
  assert.equal(count(html, /role="button" tabindex="0"/g), 1);
  assert.ok(count(html, /<g class="lin-node/g) < 40);
  assert.match(html, /viewBox="0 0 960 480"/);
});

test('rendering is bounded: 5000 nodes at any zoom stay under the node and edge caps, and a redraw is fast', () => {
  const nodes = Array.from({ length: 5000 }, (_, i) => node(`task:t${i}`, { state: i % 5 ? 'running' : 'done' }));
  const edges = nodes.slice(1).map((n, i) => edge(i % 3 ? 'dependency' : 'spawn', nodes[Math.floor(i / 3)].id, n.id));
  const g = { nodes, edges };
  const t0 = Date.now();
  const layout = layoutCached(g);
  const built = Date.now() - t0;
  assert.equal(layoutCached(g), layout, 'the layout is cached by content');
  for (const k of [ZOOM_MIN, 0.3, 1, ZOOM_MAX]) {
    const t1 = Date.now();
    const html = renderGraphSvg(g, layout, { vw: 1200, vh: 480, view: { x: 0, y: 0, k }, sel: 't1' });
    const ms = Date.now() - t1;
    const n = count(html, /<g class="lin-node/g);
    assert.ok(n >= 1 && n <= MAX_DRAWN_NODES + 2, `zoom ${k}: ${n} nodes`);
    assert.ok(count(html, /<g class="lin-edge/g) <= MAX_DRAWN_EDGES, `zoom ${k}: edges bounded`);
    assert.ok(ms < 250, `zoom ${k}: a viewport redraw took ${ms} ms`);
    assert.ok(html.length < 400000, `zoom ${k}: ${html.length} bytes of markup`);
  }
  assert.ok(built < 5000, `layout of 5000 nodes took ${built} ms`);
});

test('far out the markup drops the second line and link words; close in it has them', () => {
  const g = chain(8);
  const layout = layoutGraph(g);
  const far = renderGraphSvg(g, layout, { vw: 960, vh: 480, view: { x: 0, y: 0, k: 0.3 } });
  const near = renderGraphSvg(g, layout, { vw: 960, vh: 480, view: { x: 0, y: 0, k: 1 } });
  assert.equal(count(far, /class="lin-sub"/g), 0);
  assert.equal(count(far, /class="lin-edge-tag"/g), 0);
  assert.ok(count(near, /class="lin-sub"/g) > 0);
  assert.match(near, />handoff</);
});

test('node kinds carry a word chip and link kinds spell out handoff, branch, review and dependency', () => {
  const g = mergePages([{ nodes: ['agent:a', 'attempt:b', 'session:c', 'task:d', 'artifact:e', 'ci:f'].map((i) => node(i)), edges: [edge('fork', 'attempt:b', 'session:c')] }]);
  const html = renderGraphSvg(g, layoutGraph(g), {});
  for (const chip of ['AGT', 'ATT', 'SES', 'TSK', 'ART', 'CI']) assert.ok(html.includes(`>${chip}<`), `chip ${chip}`);
  assert.deepEqual(['handoff', 'fork', 'reviewed', 'dependency'].map(edgeLabel), ['handoff', 'branch', 'review', 'dependency']);
  assert.equal(edgeLabel('spawn'), 'spawn');
  assert.match(html, />branch</);
});

test('live and replay are told apart by a word and a glyph, in the replay bar and not by colour', () => {
  assert.match(renderModeBadge({ head: true }), /data-mode-badge="live".*Live/);
  assert.match(renderModeBadge({ head: false, step: 3, visible_events: 9 }), /data-mode-badge="replay".*Replay: event 3 of 9/);
  assert.match(renderModeBadge({ head: false, step: 0, visible_events: 9 }), /before the first event/);
  assert.match(renderModeBadge(null), /No position/);
  const bar = renderReplay({ head: false, step: 3, visible_events: 9 });
  assert.match(bar, /lin-badge--replay/);
  assert.match(bar, /class="lin-replay__status"[^>]*>Replay: event 3 of 9/, 'the status text a test and a screen reader read is unchanged');
});

test('the toolbar is plain buttons with names; focus is offered only with a selection', () => {
  const none = renderGraphToolbar({ view: { k: 0.5 }, sel: null });
  assert.match(none, /role="toolbar" aria-label="Graph view"/);
  for (const act of ['out', 'in', 'fit', 'end', 'focus']) assert.ok(none.includes(`data-vp="${act}"`));
  assert.match(none, /data-vp="focus"[^>]*disabled/);
  assert.match(none, />50%</);
  const some = renderGraphToolbar({ view: { k: 1 }, sel: 'agent:a' });
  assert.ok(!/data-vp="focus"[^>]*disabled/.test(some));
  assert.match(renderGraphToolbar({ view: { k: 1 }, focusId: 'agent:a', sel: 'agent:a' }), /data-vp="unfocus"[^>]*aria-pressed="true"/);
  assert.equal(focusKey({ getAttribute: (k) => (k === 'data-vp' ? 'fit' : null) }), 'vp:fit');
  assert.equal(focusSelector('vp:fit'), '[data-vp="fit"]');
});

test('focus keeps only the lineage of a node: ancestors and descendants, never siblings', () => {
  const g = { nodes: ['a', 'b', 'c', 'd', 'x', 'y'].map((i) => node(`task:${i}`)), edges: [edge('dependency', 'task:a', 'task:b'), edge('dependency', 'task:b', 'task:c'), edge('dependency', 'task:a', 'task:d'), edge('dependency', 'task:x', 'task:y')] };
  const f = focusSubgraph(g, 'task:b');
  assert.deepEqual(f.nodes.map((n) => n.id).sort(), ['task:a', 'task:b', 'task:c']);
  assert.equal(f.edges.length, 2);
  assert.equal(focusSubgraph(g, 'task:zzz'), g);
  assert.deepEqual(focusSubgraph(g, 'task:y').nodes.map((n) => n.id).sort(), ['task:x', 'task:y']);
});

test('keyboard: End reaches the newest session of a 60-session chain and that node can be revealed', () => {
  const g = chain(60);
  const layout = layoutGraph(g);
  const last = nextNode(layout, 'agent:lead', 'End');
  const p = layout.placed.get(last);
  const v = fitView(layout, 960, 480);
  assert.equal(isVisible(v, p, 960, 480), false, 'the newest node starts out of view');
  const c = clampView(centerOn(v, p, 960, 480), layout, 960, 480);
  assert.equal(isVisible(c, p, 960, 480), true);
  const html = renderGraphSvg(g, layout, { view: c, vw: 960, vh: 480, focus: last });
  assert.ok(html.includes(`data-node="${last}"`));
  assert.ok(count(html, /<g class="lin-node/g) <= MAX_DRAWN_NODES + 2);
});

test('the status strip reports measured counts, what needs attention and waiting approvals, and nothing when empty', () => {
  const g = { nodes: [node('task:a', { state: 'running' }), node('task:b', { state: 'failed', label: HOSTILE }), node('gate:g', { state: 'pending', label: 'Plan gate' }), node('ci:c', { state: 'blocked' }), node('task:z')], edges: [] };
  const s = statusSummary(g);
  assert.equal(s.counts.failed, 1);
  assert.deepEqual(s.approvals.map((n) => n.id), ['gate:g']);
  assert.deepEqual(s.attention.map((n) => n.id), ['task:b', 'ci:c'], 'failed first');
  const html = renderStatusStrip(g, { truncated: true });
  assert.ok(!html.includes('<img'));
  assert.match(html, /Needs attention/);
  assert.match(html, /data-node="gate:g"/);
  assert.match(html, /Counts cover the nodes loaded so far/);
  assert.equal(renderStatusStrip({ nodes: [], edges: [] }), '');
  assert.match(renderStatusStrip({ nodes: [node('task:a', { state: 'done' })], edges: [] }), /Nothing failed or blocked/);
});

// ---- inspector: only measured data --------------------------------------------------------------------------------
test('a node with nothing reported says "not reported" and shows no invented value', () => {
  const g = mergePages([{ nodes: [node('task:t', { state: 'running' })], edges: [] }]);
  const f = inspectorFacts(g, 'task:t');
  assert.equal(f.missionStatus, null);
  assert.deepEqual(f.blockers, []);
  assert.equal(f.nextAction, null);
  assert.deepEqual(f.artifacts, []);
  assert.deepEqual(f.delivery, { pr: [], ci: [], review: [] });
  assert.equal(f.handoff, null);
  const html = renderInspector(g, 'task:t');
  assert.equal(count(html, /<span class="lin-nr">not reported<\/span>/g), 7, 'mission, blockers, next action, artifacts, PR, CI, review');
  assert.ok(!/\d+\s*%/.test(html), 'no percentage anywhere');
  assert.equal(NOT_REPORTED, 'not reported');
});

test('mission status, blockers and next action come from attrs and links, and nothing else', () => {
  const g = mergePages([{ nodes: [node('mission:m', { state: 'running', label: 'Parser mission', state_reason: 'on track' }), node('task:t', { state: 'blocked', state_reason: 'waiting for review', attrs: { mission_id: 'm', next_action: 'Merge once CI is green', blockers: ['needs API key', HOSTILE] } }), node('task:dep', { state: 'running', label: 'Dep' })],
    edges: [edge('blocked_by', 'task:t', 'task:dep')] }]);
  const f = inspectorFacts(g, 'task:t');
  assert.equal(f.missionStatus.label, 'Parser mission');
  assert.equal(f.nextAction, 'Merge once CI is green');
  assert.deepEqual(f.blockers.map((b) => b.why), ['blocked by', 'reported blocker', 'reported blocker', 'state is blocked']);
  const html = renderInspector(g, 'task:t');
  assert.match(html, /Parser mission: ● running \(on track\)/);
  assert.match(html, /Merge once CI is green/);
  assert.ok(!html.includes('<img') && html.includes('&lt;img'));
  assert.ok(!html.includes('"onmouseover="'));
});

test('artifacts show version and digest from node attributes and never a storage location', () => {
  const art = node('artifact:plot', { state: 'ready', label: 'plot v3', attrs: { kind: 'figure', version_number: 3, version_id: 'ver_9', content_digests: ['sha256:' + 'ab'.repeat(32), 'sha256:' + 'cd'.repeat(32)], uri: 's3://secret-bucket/key.png', blob_url: 'https://blob.example/x', storage_path: '/vol/x', bucket: 'b' } });
  const bare = node('artifact:bare', { state: 'ready', label: 'bare' });
  const g = mergePages([{ nodes: [node('attempt:a1', { state: 'running' }), node('session:s1', { state: 'running', attrs: { attempt_id: 'a1' } }), art, bare],
    edges: [edge('produced', 'attempt:a1', 'artifact:plot'), edge('produced', 'attempt:a1', 'artifact:bare')] }]);
  const f = inspectorFacts(g, 'session:s1');
  assert.deepEqual(f.artifacts.map((x) => x.id), ['artifact:bare', 'artifact:plot'], 'a session sees what its named attempt produced');
  const plot = f.artifacts.find((x) => x.id === 'artifact:plot');
  assert.equal(plot.version, 'v3');
  assert.equal(plot.digest, 'sha256:ababa…');
  assert.equal(plot.digestCount, 2);
  const bareFact = f.artifacts.find((x) => x.id === 'artifact:bare');
  assert.equal(bareFact.version, null);
  assert.equal(bareFact.digest, null);
  for (const html of [renderInspector(g, 'session:s1'), renderInspector(g, 'artifact:plot')]) {
    for (const leak of ['s3://', 'secret-bucket', 'blob.example', '/vol/x']) assert.ok(!html.includes(leak), `${leak} is never shown`);
  }
  assert.match(renderInspector(g, 'artifact:plot'), /Storage locations are not shown/);
  assert.match(renderInspector(g, 'session:s1'), /version <span class="lin-nr">not reported<\/span> · digest <span class="lin-nr">not reported<\/span>/);
});

test('PR, CI and review are summarised from linked nodes within two links, with staleness', () => {
  const g = mergePages([{ nodes: [node('attempt:a1', { state: 'running' }), node('commit:c1', { state: 'done' }), node('pr:o/r#1', { state: 'open', label: 'PR 1', attrs: { head: 'f'.repeat(40) } }), node('ci:run1', { state: 'failed', label: 'CI run' }), node('review:r1', { state: 'done', label: 'Review' }), node('review:far', { state: 'done' })],
    edges: [edge('produced', 'attempt:a1', 'commit:c1'), edge('issue_link', 'commit:c1', 'pr:o/r#1'), edge('validated', 'ci:run1', 'pr:o/r#1', { binding: { commit: 'c1' } }), edge('reviewed', 'review:r1', 'commit:c1', { binding: { commit: 'c0' }, stale: true }), edge('dependency', 'review:far', 'review:r1')] }]);
  const f = inspectorFacts(g, 'attempt:a1');
  assert.deepEqual(f.delivery.pr.map((x) => [x.id, x.hops]), [['pr:o/r#1', 2]]);
  assert.deepEqual(f.delivery.ci, [], 'the CI run is three links from the attempt, so it is not claimed for it');
  assert.deepEqual(inspectorFacts(g, 'pr:o/r#1').delivery.ci.map((x) => [x.id, x.hops, x.state]), [['ci:run1', 1, 'failed']]);
  assert.equal(f.delivery.review.find((x) => x.id === 'review:r1').stale, true);
  assert.ok(!f.delivery.review.some((x) => x.id === 'review:far'), 'three links away is not claimed');
  const html = renderInspector(g, 'attempt:a1');
  assert.match(html, /head <code>ffffffffffff…<\/code>/);
  assert.match(html, /2 links away/);
  assert.match(html, /<strong>stale<\/strong>/);
});

test('handoff details: from/to session, checkpoint sha and cause for a session and for the handoff record', () => {
  const g = mergePages([chain(4)]);
  const f = inspectorFacts(g, 'session:s3');
  assert.equal(f.handoff.record, false);
  assert.equal(f.handoff.from[0].other, 'session:s2');
  assert.equal(f.handoff.from[0].checkpoint, 'cccccccccccc…', 'the checkpoint comes from the handoff record the edge names');
  assert.equal(f.handoff.from[0].cause, 'session_rollover');
  assert.equal(f.handoff.to[0].other, 'session:s4');
  const rec = inspectorFacts(g, 'handoff:h3');
  assert.deepEqual([rec.handoff.record, rec.handoff.from, rec.handoff.to, rec.handoff.cause, rec.handoff.state], [true, 'session:s2', 'session:s3', 'session_rollover', 'committed']);
  const html = renderInspector(g, 'session:s3');
  assert.match(html, /Received from/);
  assert.match(html, /Handed on to/);
  assert.match(html, /checkpoint <code>c+…<\/code> · cause session_rollover/);
  const first = renderInspector(g, 'session:s1');
  assert.match(first, /Received from<\/dt><dd>none recorded/);
  // a handoff edge without the record or a cause says "not reported" for the missing parts
  const bare = mergePages([{ nodes: [node('session:x'), node('session:y')], edges: [edge('handoff', 'session:x', 'session:y')] }]);
  assert.match(renderInspector(bare, 'session:y'), /checkpoint <span class="lin-nr">not reported<\/span> · cause <span class="lin-nr">not reported<\/span>/);
});

test('upstream and downstream are counted, and the terminal is offered only when the caller says so', () => {
  const g = mergePages([chain(3)]);
  const f = inspectorFacts(g, 'session:s2');
  assert.equal(f.upstream, 2);
  assert.equal(f.downstream, 1);
  const closed = renderInspector(g, 'session:s2');
  assert.match(closed, /This page offers no terminal and no commands\./);
  assert.ok(!/<a /.test(closed));
  const open = renderInspector(g, 'session:s2', null, { terminal: { href: '/#/herd/session/s2' } });
  assert.match(open, /<a class="hd-btn hd-btn--sm lin-tool" href="\/#\/herd\/session\/s2">Open this session on the dashboard<\/a>/);
  assert.ok(!/no terminal and no commands/.test(open));
  for (const bad of ['javascript:alert(1)', 'https://evil.example/', '//evil.example', '/#/other']) {
    const h = renderInspector(g, 'session:s2', null, { terminal: { href: bad } });
    assert.ok(!/<a /.test(h), `${bad} is not offered`);
    assert.match(h, /offers no terminal/);
  }
});

test('the whole panel with a 60-session chain: a bounded drawing, the status strip first, the filter and tables still there', () => {
  const g = mergePages([chain(60)]);
  const html = renderLineage({ graphAll: g, filtered: filterGraph(g, {}), asOf: { step: 9, visible_events: 9, head: true }, timeline: null, error: null, busy: false, view: 'graph', mode: 'visual', clock: 'recorded', sel: 'session:s7', focus: 'session:s7', filters: { q: '', types: new Set(), edgeTypes: new Set(), state: '' }, total: g.nodes.length, graphTruncated: null, width: 900 });
  assert.ok(html.indexOf('lin-status') < html.indexOf('lin-replay'), 'status and attention come before the controls');
  assert.ok(count(html, /<g class="lin-node/g) <= MAX_DRAWN_NODES + 2);
  assert.match(html, /data-viewport/);
  assert.match(html, /role="toolbar"/);
  assert.match(html, /Received from/);
  const table = renderLineage({ graphAll: g, filtered: filterGraph(g, {}), asOf: { step: 9, visible_events: 9, head: true }, timeline: null, error: null, busy: false, view: 'graph', mode: 'table', clock: 'recorded', sel: null, focus: null, filters: { q: '', types: new Set(), edgeTypes: new Set(), state: '' }, total: g.nodes.length, graphTruncated: null, width: 900 });
  assert.match(table, /<table class="lin-table">/);
  assert.ok(!table.includes('data-viewport'));
});

test('the Gantt marks the selected lane', () => {
  const tl = { lanes: [{ node_id: 'task:a', node_type: 'task', label: 'A', state: 'running', first_at: '2026-10-22T10:00:00Z', last_at: '2026-10-22T10:00:09Z', spans: [{ state: 'running', start: '2026-10-22T10:00:00Z', end: null, open: true, terminal: false }] }], markers: [] };
  const gantt = buildGantt(tl);
  assert.match(renderGanttSvg(gantt, { sel: 'task:a' }), /data-selected="1"/);
  assert.ok(!/data-selected/.test(renderGanttSvg(gantt, { sel: 'task:zzz' })));
});

test('viewportSize gives a small graph its own size and a large one the room the page has', () => {
  const small = layoutGraph({ nodes: [node('task:a')], edges: [] });
  assert.deepEqual(viewportSize(small, 960, 480), { w: small.width, h: 160 });
  const big = layoutGraph(chain(30));
  assert.deepEqual(viewportSize(big, 700, 360), { w: 700, h: 360 });
  assert.deepEqual(viewportSize(null, 500, 300), { w: 500, h: 300 });
  const r = visibleRect({ x: 10, y: 20, k: 2 }, 400, 200);
  assert.deepEqual(r, { x0: 10, y0: 20, x1: 210, y1: 120 });
});

// ---- S5 follow-ups: filters disclosure, minimap, operator terminal authorization ----------------------------------
import { MINIMAP_BINS, MINIMAP_H, MINIMAP_W, activeFilterCount, minimapNeeded, minimapTarget, minimapText, renderFilters, renderMinimap, renderTerminalAuth, workspaceLinkFor } from '../collab-lineage.js';

test('filters sit in a native disclosure: open or closed as the caller says, the active count is always in the summary', () => {
  const g = mergePages([chain(3)]);
  const open = renderFilters(g, {}, { open: true });
  const shut = renderFilters(g, {}, { open: false });
  assert.match(open, /^<details class="lin-filterbox" data-filterbox open><summary>Filters<span class="lin-filtercount" hidden>/);
  assert.match(shut, /^<details class="lin-filterbox" data-filterbox><summary>/);
  assert.ok(open.includes('data-lineage-filters') && shut.includes('data-lineage-filters'), 'the form is the same either way');
  const active = renderFilters(g, { q: 'x', types: new Set(['task']), edgeTypes: new Set(), state: 'done' }, { open: false });
  assert.match(active, /<span class="lin-filtercount"> \(3 active\)<\/span>/);
  assert.equal(activeFilterCount({ q: '  ' }), 0);
  assert.equal(count(renderLineage({ graphAll: g, filtered: g, asOf: null, timeline: null, error: null, busy: false, view: 'graph', mode: 'visual', clock: 'recorded', sel: null, focus: null, filters: { q: '', types: new Set(), edgeTypes: new Set(), state: '' }, filtersOpen: false, total: 1, graphTruncated: null, width: 900 }), /<details class="lin-filterbox" data-filterbox>/g), 1, 'the panel passes the choice through (closed)');
});

test('minimap: bounded however big the graph, a text alternative, not focusable, nothing animated, absent when the whole graph is in view', () => {
  const big = { nodes: [], edges: [] };
  for (let i = 0; i < 3000; i += 1) { big.nodes.push(node(`task:t${i}`)); if (i) big.edges.push(edge('dependency', `task:t${Math.floor((i - 1) / 2)}`, `task:t${i}`)); }
  const lay = layoutGraph(big);
  const v = fitView(lay, 900, 480);
  const html = renderMinimap(lay, v, 900, 480);
  assert.ok(minimapNeeded(lay, v, 900, 480));
  assert.ok(count(html, /<rect class="lin-mm-bar"/g) <= MINIMAP_BINS, `${count(html, /<rect class="lin-mm-bar"/g)} bars`);
  assert.equal(count(html, /<rect class="lin-mm-view"/g), 1);
  assert.match(html, new RegExp(`width="${MINIMAP_W}" height="${MINIMAP_H}"`));
  assert.match(html, /role="img" aria-label="Overview map: the view shows columns 1 to \d+ of \d+ and rows 1 to \d+ of \d+\./);
  assert.ok(!/tabindex|<button|<a |<animate|<set |transition/.test(html), 'no focus stop, no control, no animation');
  assert.match(minimapText(lay, { ...v, x: 5000 }, 900, 480), /columns (?!1 to)\d+ to \d+ of \d+/, 'the text follows the view');
  const small = layoutGraph(mergePages([chain(2)]));
  assert.equal(renderMinimap(small, fitView(small, 900, 480), 900, 480), '', 'nothing to overview when everything is in view');
  const t = minimapTarget(lay, 0.5, 2);
  assert.equal(t.x, lay.width / 2); assert.equal(t.y, lay.height, 'a click is clamped to the layout');
});

test('minimap viewport rectangle follows the view and stays inside the map', () => {
  const lay = layoutGraph(mergePages([chain(60)]));
  for (const x of [-500, 0, 3000, 13000, 99999]) {
    const html = renderMinimap(lay, { x, y: 0, k: 1 }, 900, 480);
    const m = html.match(/class="lin-mm-view" x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"/);
    assert.ok(m, `view rect for x=${x}`);
    const [, rx, ry, rw, rh] = m.map(Number);
    assert.ok(rx >= 0 && ry >= 0 && rx + rw <= MINIMAP_W + 0.05 && ry + rh <= MINIMAP_H + 0.05, `inside the map at x=${x}: ${m[0]}`);
  }
});

test('terminal authorization control: only for a link the server said the viewer may use; the node must map to exactly one link', () => {
  const links = [{ link_id: 'wl_1', workspace_id: 'ws-1', host: 'bender', can_terminal: true }, { link_id: 'wl_2', workspace_id: 'ws-2', host: 'bender', can_terminal: false }];
  const ws = node('workspace:ws-1');
  assert.equal(workspaceLinkFor(links, ws).link_id, 'wl_1');
  assert.equal(workspaceLinkFor(links, node('session:x', { attrs: { workspace_id: 'ws-1', host: 'bender' } })).link_id, 'wl_1');
  assert.equal(workspaceLinkFor(links, node('session:x', { attrs: { workspace_id: 'ws-1', host: 'beast' } })), null, 'a different host is not this link');
  assert.equal(workspaceLinkFor(links, node('session:x')), null);
  assert.equal(workspaceLinkFor(null, ws), null);
  assert.equal(workspaceLinkFor([...links, { ...links[0], link_id: 'wl_3' }], ws), null, 'ambiguous: no guess');
  const html = renderTerminalAuth(links[0]);
  assert.match(html, /<button type="button" class="hd-btn hd-btn--sm lin-tool" data-term-auth="wl_1">Check terminal access for ws-1<\/button>/);
  assert.equal(renderTerminalAuth(links[1]), '', 'can_terminal false: not rendered');
  assert.equal(renderTerminalAuth({ ...links[0], can_terminal: 'yes' }), '', 'only a literal true');
  assert.equal(renderTerminalAuth(null), '');
  assert.match(renderTerminalAuth(links[0], { link_id: 'wl_1', ok: true }), /role="status">The server allows you the terminal of ws-1 on bender/);
  assert.match(renderTerminalAuth(links[0], { link_id: 'wl_1', ok: false, reason: 'HTTP 403' }), /role="alert">The server did not allow the terminal \(HTTP 403\)/);
  assert.match(renderTerminalAuth(links[0], null, true), /disabled aria-disabled="true"/);
  const g = mergePages([{ nodes: [ws], edges: [] }]);
  assert.ok(!/data-term-auth/.test(renderInspector(g, 'workspace:ws-1')), 'without termAuth (anyone but the operator) nothing is rendered');
  assert.match(renderInspector(g, 'workspace:ws-1', null, { termAuth: { link: links[0], result: null, busy: false } }), /data-term-auth="wl_1"/);
  const evil = renderTerminalAuth({ link_id: HOSTILE, workspace_id: HOSTILE, host: HOSTILE, can_terminal: true });
  assert.ok(!/<img/.test(evil) && /&lt;img/.test(evil), 'hostile ids are escaped');
});
