import test from 'node:test';
import assert from 'node:assert/strict';
import {
  EDGE_STYLE, MAX_RENDER_NODES, buildGantt, filterGraph, fmtDuration, layoutGraph, mergePages, nextNode,
  renderGanttSvg, focusKey, focusSelector, renderGanttTable, renderGraphSvg, renderGraphTable, renderInspector, renderLineage, renderReplay,
  nodeLabel, replayTarget, replayText, shapeOf, stateLook,
} from '../collab-lineage.js';

const node = (id, extra = {}) => ({ id, node_type: id.split(':')[0], label: extra.label ?? id.split(':')[1], state: extra.state ?? null, attrs: extra.attrs || {}, visibility_scope: 'room', ...extra });
const edge = (t, a, b, extra = {}) => ({ edge_type: t, from_id: a, to_id: b, attrs: {}, stale: false, removed: false, ...extra });
const HOSTILE = '<img src=x onerror=alert(1)>"onmouseover="x\'';
const sample = () => mergePages([{
  nodes: [node('agent:lead', { state: 'running' }), node('agent:kid', { state: 'done' }), node('attempt:a1', { state: 'failed' }),
    node('pr:github.com/o/r#1', { label: 'PR 1', state: 'open' }), node('review:r1')],
  edges: [edge('spawn', 'agent:lead', 'agent:kid'), edge('spawn', 'agent:kid', 'attempt:a1'),
    edge('reviewed', 'review:r1', 'pr:github.com/o/r#1', { binding: { commit: 'c1' }, stale: true })],
}]);

test('pages merge by identity and later pages win; bad edges are dropped', () => {
  const m = mergePages([{ nodes: [node('agent:a', { label: 'old' })], edges: [edge('spawn', 'agent:a', 'agent:b')] },
    { nodes: [node('agent:a', { label: 'new' }), node('agent:b')], edges: [edge('spawn', 'agent:a', 'agent:b'), edge('friendship', 'agent:a', 'agent:b'), { edge_type: 'spawn' }] }]);
  assert.equal(m.nodes.length, 2);
  assert.equal(m.nodes.find((n) => n.id === 'agent:a').label, 'new');
  assert.equal(m.edges.length, 1);
  assert.deepEqual(mergePages(null), { nodes: [], edges: [] });
});

test('reviewed edges for different commits are distinct edges', () => {
  const m = mergePages([{ nodes: [], edges: [edge('reviewed', 'review:r', 'pr:p', { binding: { commit: 'c1' } }), edge('reviewed', 'review:r', 'pr:p', { binding: { commit: 'c2' } })] }]);
  assert.equal(m.edges.length, 2);
});

test('filters by type, link type and state; search keeps one hop of context and flags hits', () => {
  const g = sample();
  assert.deepEqual(filterGraph(g, { types: new Set(['agent']) }).nodes.map((n) => n.id).sort(), ['agent:kid', 'agent:lead']);
  assert.equal(filterGraph(g, { types: new Set(['agent']) }).edges.length, 1);
  assert.equal(filterGraph(g, { edgeTypes: new Set(['reviewed']) }).edges.length, 1);
  assert.deepEqual(filterGraph(g, { state: 'failed' }).nodes.map((n) => n.id), ['attempt:a1']);
  const hit = filterGraph(g, { q: 'KID' });
  assert.deepEqual([...hit.match], ['agent:kid']);
  assert.deepEqual(hit.nodes.map((n) => n.id).sort(), ['agent:kid', 'agent:lead', 'attempt:a1']);
  assert.equal(filterGraph(g, { q: 'zzz' }).nodes.length, 0);
});

test('layout is layered left to right, deterministic, and cuts cycles without dropping nodes', () => {
  const l = layoutGraph(sample());
  assert.equal(l.placed.get('agent:lead').level, 0);
  assert.equal(l.placed.get('agent:kid').level, 1);
  assert.equal(l.placed.get('attempt:a1').level, 2);
  assert.equal(JSON.stringify([...layoutGraph(sample()).placed]), JSON.stringify([...l.placed]));
  const cyc = layoutGraph({ nodes: [node('task:a'), node('task:b'), node('task:c')], edges: [edge('dependency', 'task:a', 'task:b'), edge('dependency', 'task:b', 'task:c'), edge('dependency', 'task:c', 'task:a')] });
  assert.equal(cyc.placed.size, 3);
  const self = layoutGraph({ nodes: [node('task:a')], edges: [edge('dependency', 'task:a', 'task:a')] });
  assert.equal(self.placed.size, 1);
  assert.deepEqual(layoutGraph({ nodes: [], edges: [] }).columns, []);
});

test('arrow keys move between nodes; Home and End jump', () => {
  const l = layoutGraph(sample());
  assert.equal(nextNode(l, 'agent:lead', 'ArrowRight'), 'agent:kid');
  assert.equal(nextNode(l, 'agent:kid', 'ArrowLeft'), 'agent:lead');
  assert.equal(nextNode(l, 'agent:lead', 'ArrowLeft'), null);
  assert.equal(nextNode(l, 'attempt:a1', 'ArrowRight'), null);
  const col0 = l.columns[0];
  assert.equal(nextNode(l, col0[0], 'ArrowDown'), col0[1]);
  assert.equal(nextNode(l, col0[1], 'ArrowUp'), col0[0]);
  assert.equal(nextNode(l, col0[0], 'Home'), l.columns.flat()[0]);
  assert.equal(nextNode(l, col0[0], 'End'), l.columns.flat().at(-1));
  assert.equal(nextNode(layoutGraph({ nodes: [], edges: [] }), 'x', 'Home'), null);
});

test('the SVG graph is escaped, labelled, keyboard operable and not colour-only', () => {
  const g = mergePages([{ nodes: [node('agent:a', { label: HOSTILE, state: 'running' }), node(`task:${HOSTILE}`, { label: HOSTILE, state: 'failed' })], edges: [edge('dependency', 'agent:a', `task:${HOSTILE}`, { stale: true })] }]);
  const html = renderGraphSvg(g, layoutGraph(g), { sel: 'agent:a' });
  assert.ok(!html.includes('<img') && !html.includes('onerror=alert(1)>"') && !/ onmouseover=/.test(html));
  assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert.equal([...html.matchAll(/<g class="lin-node/g)].length, 2);
  assert.equal([...html.matchAll(/role="button" tabindex="0"/g)].length, 1, 'roving tabindex: exactly one tab stop');
  assert.equal([...html.matchAll(/role="button" tabindex="-1"/g)].length, 1);
  assert.match(html, /aria-pressed="true"/);
  assert.match(html, /aria-label="agent .*running, 1 outgoing and 0 incoming links\. Press Enter to inspect\./);
  assert.match(html, /stroke-dasharray="5 5"/, 'dependency is dashed');
  assert.match(html, /needs \(stale\)/, 'text tag, not colour, says stale');
  assert.match(html, /●/);
  assert.match(html, /✕/);
  assert.match(html, /role="group" aria-label="Lineage graph: 2 nodes and 1 links/);
});

test('every link type has a distinct dash/tag pair and node types map to shapes', () => {
  const pairs = Object.values(EDGE_STYLE).map((s) => `${s.dash}|${s.tag}`);
  assert.equal(new Set(pairs).size, pairs.length);
  assert.equal(new Set(Object.values(EDGE_STYLE).map((s) => s.tag)).size, Object.keys(EDGE_STYLE).length);
  assert.deepEqual(['agent', 'task', 'pr', 'review', 'handoff', 'nothing'].map(shapeOf), ['round', 'rect', 'tag', 'diamond', 'hex', 'rect']);
  assert.equal(stateLook('running').glyph, '●');
  assert.equal(stateLook(null).word, 'no state recorded');
  assert.equal(stateLook('weird').kind, 'other');
});

test('a very large graph is not drawn as SVG but stays fully available as a table', () => {
  const nodes = Array.from({ length: MAX_RENDER_NODES + 5 }, (_, i) => node(`task:t${i}`));
  const g = { nodes, edges: [] };
  const svg = renderGraphSvg(g, layoutGraph(g));
  assert.ok(!svg.includes('<svg'));
  assert.match(svg, /more than the 300 drawn/);
  assert.equal([...renderGraphTable(g).matchAll(/<tr/g)].length, nodes.length + 2 + 1, 'header rows plus a no-links row');
});

test('the table view is a real table with captions, scopes and escaped cells', () => {
  const g = mergePages([{ nodes: [node('agent:a', { label: HOSTILE }), node('pr:p')], edges: [edge('reviewed', 'agent:a', 'pr:p', { binding: { commit: 'c"1' }, stale: true })] }]);
  const html = renderGraphTable(g, { sel: 'agent:a' });
  assert.match(html, /<caption>2 nodes<\/caption>/);
  assert.match(html, /<th scope="col">Name<\/th>/);
  assert.match(html, /<th scope="row"><button/);
  assert.match(html, /aria-current="true"/);
  assert.ok(html.includes('commit=c&quot;1'));
  assert.match(html, /<td>stale<\/td>/);
  assert.ok(!html.includes('<img'));
});

test('replay controls: a range with spoken value text, step buttons, live button, and clamped targets', () => {
  const asOf = { step: 3, visible_events: 10, head: false, received_at: '2026-10-22T10:00:03+00:00', window_trimmed: false };
  const html = renderReplay(asOf);
  assert.match(html, /type="range" data-replay="slider" min="0" max="10" step="1" value="3" aria-valuetext="Replay: event 3 of 10/);
  assert.match(html, /role="status" aria-live="polite"/);
  for (const a of ['first', 'prev', 'next', 'last', 'live']) assert.match(html, new RegExp(`data-replay="${a}"`));
  assert.equal(replayTarget(asOf, 'first'), 0);
  assert.equal(replayTarget(asOf, 'prev'), 2);
  assert.equal(replayTarget(asOf, 'next'), 4);
  assert.equal(replayTarget({ ...asOf, step: 9 }, 'next'), 'live');
  assert.equal(replayTarget({ ...asOf, step: 0 }, 'prev'), 0);
  assert.equal(replayTarget(asOf, 'slider', '10'), 'live');
  assert.equal(replayTarget(asOf, 'slider', '-4'), 0);
  assert.equal(replayTarget(asOf, 'slider', '4.9'), 4);
  assert.equal(replayTarget(asOf, 'slider', 'junk'), 3);
  assert.match(replayText({ step: 10, visible_events: 10, head: true }), /^Live/);
  assert.match(replayText({ step: 0, visible_events: 4, head: false }), /before the first/);
  assert.match(renderReplay({ ...asOf, window_trimmed: true }), /Only the newest events you can see/);
  assert.doesNotMatch(renderReplay(asOf, { busy: true }), /disabled/, 'busy keeps focus on a live control');
  assert.match(renderReplay(asOf, { busy: true }), /aria-busy="true"/);
  assert.match(renderReplay(null), /max="0"[^>]*disabled/);
});

const TL = {
  as_of: { step: 5, visible_events: 5, head: true },
  lanes: [
    { node_id: 'attempt:a1', node_type: 'attempt', label: 'Attempt <1>', state: 'done', first_at: '2026-10-22T10:00:00+00:00', last_at: '2026-10-22T10:10:00+00:00',
      spans: [{ state: 'running', start: '2026-10-22T10:00:00+00:00', claimed_start: '2026-10-22T09:59:00+00:00', end: '2026-10-22T10:05:00+00:00', claimed_end: '2026-10-22T10:04:00+00:00', open: false, terminal: false, reason: null },
        { state: 'blocked', start: '2026-10-22T10:05:00+00:00', claimed_start: null, end: '2026-10-22T10:10:00+00:00', open: false, terminal: false, reason: 'waiting <b>x</b>' },
        { state: 'done', start: '2026-10-22T10:10:00+00:00', end: '2026-10-22T10:10:00+00:00', open: false, terminal: true }] },
    { node_id: 'session:s1', node_type: 'session', label: 's1', state: 'running', first_at: '2026-10-22T10:02:00+00:00', last_at: '2026-10-22T10:02:00+00:00',
      spans: [{ state: 'running', start: '2026-10-22T10:02:00+00:00', end: null, open: true, terminal: false }] },
    { node_id: 'task:t', node_type: 'task', label: 't', state: null, first_at: '2026-10-22T10:03:00+00:00', last_at: '2026-10-22T10:03:00+00:00', spans: [] },
  ],
  markers: [
    { kind: 'edge', step: 2, at: '2026-10-22T10:01:00+00:00', claimed: null, edge_type: 'spawn', from_id: 'agent:lead', to_id: 'attempt:a1', binding: null, stale: null },
    { kind: 'edge', step: 3, at: '2026-10-22T10:06:00+00:00', claimed: null, edge_type: 'reviewed', from_id: 'review:r', to_id: 'pr:p', binding: { commit: 'c1' }, stale: true },
    { kind: 'head_change', step: 4, at: '2026-10-22T10:07:00+00:00', claimed: null, edge_type: 'reviewed', from_id: 'review:r', to_id: 'pr:p', binding: { commit: 'c2' }, stale: false },
  ],
  truncated: false, truncated_reason: null,
};

test('Gantt: open spans run to the end of the range, instants stay instants, claimed clock falls back to recorded', () => {
  const g = buildGantt(TL);
  assert.equal(g.lanes.length, 3);
  const open = g.lanes[1].spans[0];
  assert.equal(open.endMs, g.max);
  assert.equal(g.min, Date.parse('2026-10-22T10:00:00+00:00'));
  assert.equal(g.markers.length, 3);
  const rep = buildGantt(TL, { clock: 'reported' });
  assert.equal(rep.lanes[0].spans[0].startMs, Date.parse('2026-10-22T09:59:00+00:00'));
  assert.equal(rep.lanes[0].spans[1].startMs, Date.parse('2026-10-22T10:05:00+00:00'), 'no claim: recorded time');
  assert.equal(buildGantt(TL, { types: new Set(['task']) }).lanes.length, 1);
  assert.equal(buildGantt(null).lanes.length, 0);
  assert.equal(buildGantt({ lanes: [{ node_id: 'task:x', node_type: 'task', spans: [{ state: 'x', start: 'not a time' }] }], markers: [{ at: 'bad' }] }).markers.length, 0);
});

test('Gantt SVG: patterns and words besides colour, head-change and stale markers, escaped labels, text alternative', () => {
  const g = buildGantt(TL);
  const svg = renderGanttSvg(g, { width: 900 });
  assert.ok(!svg.includes('Attempt <1>') && svg.includes('Attempt &lt;1&gt;'));
  for (const p of ['lin-p-active', 'lin-p-waiting', 'lin-p-finished']) assert.ok(svg.includes(`url(#${p})`));
  assert.match(svg, /ongoing/);
  assert.match(svg, />H<\/text>/, 'head change carries a letter');
  assert.match(svg, />stale<\/text>/, 'stale carries a word');
  assert.match(svg, /role="img" aria-label="Gantt timeline of 3 items and 3 link events, recorded time\. A table below lists the same data\./);
  assert.match(svg, /<title>Attempt &lt;1&gt;: running/);
  assert.match(renderGanttSvg(buildGantt({ lanes: [], markers: [] })), /No timeline events yet/);
});

test('Gantt table lists every span with duration and notes, escaped', () => {
  const html = renderGanttTable(buildGantt(TL));
  assert.match(html, /<caption>3 items, recorded time<\/caption>/);
  assert.match(html, /5 min/);
  assert.match(html, /ongoing/);
  assert.ok(html.includes('waiting &lt;b&gt;x&lt;/b&gt;') && !html.includes('<b>x'));
  assert.match(html, /head change/);
  assert.match(html, /<td>current<\/td>/);
  assert.match(renderGanttTable({ ...buildGantt(TL), truncated: true, reason: 'lane_limit' }), /lane_limit/);
});

test('durations read plainly', () => {
  assert.deepEqual([0, 900, 5000, 120000, 7200000, 3 * 86400000].map(fmtDuration), ['0 ms', '900 ms', '5 s', '2 min', '2 h', '3 d']);
  assert.equal(fmtDuration(-1), 'n/a');
  assert.equal(fmtDuration(NaN), 'n/a');
});

test('inspector escapes everything, links neighbours as buttons, shows stale and has no terminal', () => {
  const g = mergePages([{ nodes: [node('pr:p', { label: HOSTILE, state: 'open', attrs: { url: 'javascript:alert(1)', blob: { a: '<script>' } }, fields: { label: { writer: '<w>', source: 's"x' } }, first_event_id: 'e1', last_event_id: 'e2' }), node('review:r', { label: 'R' })],
    edges: [edge('reviewed', 'review:r', 'pr:p', { binding: { commit: 'c1' }, stale: true, writer: 'key:w' })] }]);
  const html = renderInspector(g, 'pr:p', TL);
  assert.ok(!html.includes('<script') && !html.includes('<img') && !html.includes('<w>'));
  assert.ok(html.includes('javascript:alert(1)') && !/href="javascript/.test(html), 'values are text, never links');
  assert.match(html, /data-node="review:r"/);
  assert.match(html, /<strong>stale<\/strong>/);
  assert.match(html, /Read-only\. This page offers no terminal and no commands\./);
  assert.ok(!/<a |terminal</i.test(html.replace('no terminal and no commands', '')));
  assert.match(renderInspector(g, 'nope'), /Select a node to inspect it/);
  assert.match(renderInspector(mergePages([{ nodes: [node('attempt:a1')], edges: [] }]), 'attempt:a1', TL), /State history/);
});

test('the filter form has no inline handler (the page CSP forbids it) and says what it is for', () => {
  const html = renderLineage({ graphAll: sample(), filtered: filterGraph(sample(), {}), asOf: { step: 1, visible_events: 1, head: true }, timeline: null, error: null, busy: false, view: 'graph', mode: 'visual', clock: 'recorded', sel: null, focus: null, filters: { q: '', types: new Set(), edgeTypes: new Set(), state: '' }, total: 5, graphTruncated: null, width: 800 });
  assert.ok(!/ on[a-z]+=/i.test(html.replace(/<svg[\s\S]*<\/svg>/, '')));
  assert.match(html, /<form class="lin-filters cx-stack" role="search" aria-label="Filter the lineage"/);
});

test('the whole panel renders live, replayed, errored, empty and in both modes', () => {
  const graphAll = sample();
  const base = { graphAll, filtered: filterGraph(graphAll, {}), asOf: { step: 2, visible_events: 5, head: false }, timeline: TL, error: null, busy: false, view: 'graph', mode: 'visual', clock: 'recorded', sel: 'agent:lead', focus: null, filters: { q: '', types: new Set(), edgeTypes: new Set(), state: '' }, total: 5, graphTruncated: null, width: 800 };
  const html = renderLineage(base);
  assert.match(html, /role="tablist" aria-label="Lineage views"/);
  assert.match(html, /data-view="graph"[^>]*aria-selected="true"/);
  assert.match(html, /<aside[^>]*aria-label="Inspector"/);
  assert.match(html, /Replay: event 2 of 5/);
  assert.match(renderLineage({ ...base, mode: 'table' }), /<caption>5 nodes<\/caption>/);
  const gantt = renderLineage({ ...base, view: 'gantt' });
  assert.match(gantt, /data-clock/);
  assert.match(gantt, /<svg class="lin-svg"[^>]*role="img"/);
  assert.match(gantt, /Timeline table/);
  assert.match(renderLineage({ ...base, view: 'gantt', mode: 'table' }), /Timeline table/);
  assert.ok(!renderLineage({ ...base, view: 'gantt', mode: 'table' }).includes('role="img"'));
  assert.match(renderLineage({ ...base, graphTruncated: 'limit' }), /data-more="1"/);
  assert.match(renderLineage({ ...base, error: 'Nope <b>' }), /role="alert">Nope &lt;b&gt;/);
  const empty = renderLineage({ ...base, graphAll: { nodes: [], edges: [] }, filtered: { nodes: [], edges: [], match: new Set() }, timeline: null });
  assert.match(empty, /No lineage is shown for this room yet/);
});

test('the lineage calls are one room, read-only GETs on the collab route', async () => {
  const { CollabApi } = await import('./collab.js');
  const calls = [];
  const api = new CollabApi(() => ({ access: 'T' }), async (url, init) => { calls.push([init.method, url]); return { status: 200, ok: true, json: async () => ({}) }; });
  await api.lineage('room 1');
  await api.lineage('room 1', { step: 4, offset: 200, nodeTypes: 'agent,pr' });
  await api.lineage('room 1', { step: 0 });
  await api.lineage('room 1', { at: '2026-10-22T10:00:00Z' });
  await api.lineageTimeline('room 1', { step: 7 });
  await api.lineageEvents('room 1', { cursor: 200, limit: 100 });
  assert.deepEqual(calls.map((c) => c[0]), ['GET', 'GET', 'GET', 'GET', 'GET', 'GET']);
  assert.equal(calls[0][1], '/api/v1/collab/graph/lineage?room_id=room+1&limit=200');
  assert.equal(calls[1][1], '/api/v1/collab/graph/lineage?room_id=room+1&step=4&limit=200&offset=200&node_types=agent%2Cpr');
  assert.equal(calls[2][1], '/api/v1/collab/graph/lineage?room_id=room+1&step=0&limit=200');
  assert.equal(calls[3][1], '/api/v1/collab/graph/lineage?room_id=room+1&at=2026-10-22T10%3A00%3A00Z&limit=200');
  assert.equal(calls[4][1], '/api/v1/collab/graph/lineage/timeline?room_id=room+1&step=7');
  assert.equal(calls[5][1], '/api/v1/collab/graph/lineage/events?room_id=room+1&limit=100&cursor=200');
});

test('focus keys round-trip to a selector for every control kind', () => {
  const el = (a) => ({ getAttribute: (k) => (k in a ? a[k] : null) });
  for (const [attrs, key, sel] of [
    [{ 'data-replay': 'prev' }, 'replay:prev', '[data-replay="prev"]'],
    [{ 'data-node': 'task:a"b' }, 'node:task:a"b', '[data-node="task:a\\"b"]'],
    [{ 'data-view': 'gantt' }, 'view:gantt', '[data-view="gantt"]'],
    [{ 'data-mode': 'table' }, 'mode:', '[data-mode]'],
    [{ 'data-filter': 'type', value: 'task' }, 'filter:type:task', '[data-filter="type"][value="task"]'],
    [{ 'data-filter': 'q' }, 'filter:q:', '[data-filter="q"]'],
    [{ 'data-clock': '' }, 'clock:', '[data-clock]'],
    [{ 'data-more': '1' }, 'more:', '[data-more]'],
    [{ 'data-retry': '1' }, 'retry:', '[data-retry]'],
  ]) { assert.equal(focusKey(el(attrs)), key); assert.equal(focusSelector(key), sel); }
  assert.equal(focusKey(el({})), null);
  assert.equal(focusSelector('nonsense:x'), null);
});

test('an error still renders the replay controls and a Retry button; no false "will be retried" claim', () => {
  const html = renderLineage({ graphAll: { nodes: [], edges: [] }, filtered: { nodes: [], edges: [], match: new Set() }, asOf: { step: 3, visible_events: 5, head: false }, error: 'Could not load the lineage. Press Retry, or choose Live.', filters: { types: new Set(), edgeTypes: new Set() } });
  assert.match(html, /data-replay="live"/);
  assert.match(html, /data-retry/);
  assert.doesNotMatch(html, /will be retried/);
});

test('node aria-labels are clipped', () => {
  assert.ok(nodeLabel({ node_type: 'task', id: 'task:x', label: 'L'.repeat(900), state: 'running' }, { in: 1, out: 2 }).length <= 120);
});

test('the replay status says a choice is queued while loading', () => {
  assert.match(renderReplay({ step: 1, visible_events: 5, head: false }, { busy: true, queued: true }), /latest choice will load next/);
  assert.match(renderReplay({ step: 1, visible_events: 5, head: false }, { busy: true }), /Loading the position/);
  assert.match(renderLineage({ graphAll: { nodes: [], edges: [] }, filtered: { nodes: [], edges: [], match: new Set() }, asOf: { step: 5, visible_events: 5, head: true }, notice: 'That position could not be loaded, so Live is shown.', filters: { types: new Set(), edgeTypes: new Set() } }), /so Live is shown/);
});
