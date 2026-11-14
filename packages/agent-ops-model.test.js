import test from 'node:test';
import assert from 'node:assert/strict';
import {
  agentOpsEnabled, altTable, buildTimeline, contextInfo, cursorTime, describeEdge, describeNode, diffSnapshots, edgeId, edgeStyle, filterOptions, filterSnapshot,
  indexAtFraction, layoutScene, hostStrip, driftItems, TIMELINE_CAP, modeKinds, neighbour, nextIndex, replayFrame, ringGeometry, stateInfo, stepDelay, NODE_R, RING_R, HALO_R, estimateTextWidth,
} from './agent-ops-model.js';

const agent = (id, over = {}) => ({ node_id: id, agent_id: id, session_id: id, host_id: 'h1', provider: null, model: 'opus', model_changes: [], state: 'idle',
  context: { used: 10, limit: 100, source: 'pi' }, room_ids: [], parent_session_id: null, compactions: 0, drift_warnings: 0, label: null, provenance: 'observed', last_event_at: null, ...over });
const edge = (kind, from, to, state = 'accepted', over = {}) => ({ kind, from, to, state, room_id: null, event_id: `e-${kind}-${from}-${to}`, provenance: 'observed', task_id: null, handoff_id: null, updated_at: 't', ...over });
const SNAP = {
  agents: [agent('s3', { room_ids: ['r1'], model: 'qwen', host_id: 'h2', provider: 'local' }), agent('s1', { room_ids: ['r1'] }), agent('s2', { room_ids: ['r2'], context: null, model: null }), agent('a9', { state: 'unknown', context: null })],
  rooms: [{ room_id: 'r2', agent_ids: ['s2'] }, { room_id: 'r1', agent_ids: ['s1', 's3'] }],
  edges: [edge('delegation', 's1', 's2', 'planned', { task_id: 't1' }), edge('communication', 's1', 's3'), edge('communication', 's3', 's1'), edge('ownership', 'user:alice', 's1', 'transferred', { task_id: 't1' })],
  tasks: [{ task_id: 't1', room_id: null, accountable_owner: 's1', contributors: ['s2'], reviewer: 's3', updated_at: 't' }],
  cursor: '2026-11-12T10:00:04.000000Z~e4', counts: {}, truncated: false,
};

test('capability gate is a strict membership check', () => {
  assert.equal(agentOpsEnabled(['collab_v1', 'agent_ops_v1']), true);
  assert.equal(agentOpsEnabled(['collab_v1']), false);
  assert.equal(agentOpsEnabled(undefined), false);
});

test('layout is deterministic and independent of input order', () => {
  const a = layoutScene(SNAP);
  const shuffled = { ...SNAP, agents: [...SNAP.agents].reverse(), rooms: [...SNAP.rooms].reverse(), edges: [...SNAP.edges].reverse() };
  const b = layoutScene(shuffled);
  const pos = (s) => Object.fromEntries(s.nodes.map((n) => [n.id, [n.x, n.y, n.room]]));
  assert.deepEqual(pos(a), pos(b));
  assert.deepEqual(layoutScene(SNAP).nodes.map((n) => [n.id, n.x, n.y]), a.nodes.map((n) => [n.id, n.x, n.y]));
});

test('rooms are clusters: members share a room, no-room agents get their own cluster, a person is a stub node', () => {
  const s = layoutScene(SNAP);
  assert.deepEqual(s.rooms.map((r) => r.id), ['r1', 'r2', '']);
  assert.equal(s.nodes.find((n) => n.id === 's3').room, 'r1');
  assert.equal(s.nodes.find((n) => n.id === 'a9').room, '');
  const alice = s.nodes.find((n) => n.id === 'user:alice');
  assert.ok(alice.person);
  assert.equal(alice.name, 'alice');
  for (const n of s.nodes) {
    const r = s.rooms.find((x) => x.id === n.room);
    assert.ok(Math.hypot(n.x - r.cx, n.y - r.cy) + NODE_R <= r.r, `${n.id} inside its cluster`);
  }
});

test('nodes never overlap and everything sits inside the viewBox', () => {
  const many = { ...SNAP, agents: Array.from({ length: 14 }, (_, i) => agent(`n${String(i).padStart(2, '0')}`, { room_ids: [i % 3 ? 'r1' : 'r2'] })), edges: [] };
  const s = layoutScene(many), vb = s.viewBox;
  for (const a of s.nodes) {
    assert.ok(a.x - NODE_R >= vb.x && a.x + NODE_R <= vb.x + vb.w && a.y - NODE_R >= vb.y && a.y + NODE_R <= vb.y + vb.h, `${a.id} in viewBox`);
    for (const b of s.nodes) if (a.id < b.id) assert.ok(Math.hypot(a.x - b.x, a.y - b.y) > 2 * RING_R, `${a.id}/${b.id} apart`);
  }
});

test('empty snapshot gives an empty scene with a usable viewBox', () => {
  const s = layoutScene({ agents: [], edges: [], rooms: [], tasks: [] });
  assert.equal(s.nodes.length, 0);
  assert.ok(s.viewBox.w > 0 && s.viewBox.h > 0);
});

test('unknown context is explicit and never 0%', () => {
  const c = contextInfo(null);
  assert.equal(c.label, 'ctx unknown');
  assert.equal(c.fraction, null);
  assert.equal(c.percent, null);
  assert.equal(c.level, 'unknown');
  assert.equal(contextInfo(undefined).label, 'ctx unknown');
  const s = layoutScene(SNAP);
  const n = s.nodes.find((x) => x.id === 's2');
  assert.equal(n.ctx.label, 'ctx unknown');
  assert.equal(n.ring.unknown, true);
  assert.equal(n.ring.dash, 0);
  assert.doesNotMatch(describeNode(n), /0%/);
  assert.match(describeNode(n), /context unknown/);
});

test('measured zero is a real 0%, but a missing limit is not a fraction', () => {
  assert.equal(contextInfo({ used: 0, limit: 100 }).label, '0% ctx');
  const noLimit = contextInfo({ used: 5, limit: null });
  assert.equal(noLimit.fraction, null);
  assert.match(noLimit.label, /5 used/);
  assert.equal(contextInfo({ used: 5, limit: 0 }).fraction, null);
});

test('context levels switch at 70 and 90 and carry text, not colour alone', () => {
  assert.equal(contextInfo({ used: 69, limit: 100 }).level, 'ok');
  const warn = contextInfo({ used: 70, limit: 100 });
  assert.equal(warn.level, 'warn');
  assert.match(warn.label, /70% ctx high/);
  const crit = contextInfo({ used: 90, limit: 100 });
  assert.equal(crit.level, 'critical');
  assert.match(crit.label, /critical/);
  assert.equal(contextInfo({ used: 150, limit: 100 }).fraction, 1);
  assert.equal(contextInfo({ used: 150, limit: 100 }).percent, 150);
});

test('ring geometry fills the measured fraction of the circumference', () => {
  const g = ringGeometry(0.25);
  assert.ok(Math.abs(g.dash - g.circumference / 4) < 0.01);
  assert.ok(Math.abs(g.dash + g.gap - g.circumference) < 0.02);
  assert.equal(ringGeometry(null).unknown, true);
});

test('state halo: known states map, anything else is explicitly unknown', () => {
  assert.equal(stateInfo('working').tone, 'active');
  assert.equal(stateInfo('attention').label, 'Needs attention');
  assert.equal(stateInfo('weird').key, 'unknown');
  assert.equal(stateInfo(null).label, 'State unknown');
});

test('edge style by kind: dashed = planned, solid = accepted, dotted = rejected, always with a text label', () => {
  const planned = edgeStyle(edge('delegation', 'a', 'b', 'planned'));
  assert.deepEqual([planned.dashed, planned.line, planned.label], [true, 'dashed', 'Delegation · planned']);
  const accepted = edgeStyle(edge('delegation', 'a', 'b', 'accepted'));
  assert.deepEqual([accepted.dashed, accepted.dash, accepted.line], [false, null, 'solid']);
  const rejected = edgeStyle(edge('handoff', 'a', 'b', 'rejected'));
  assert.deepEqual([rejected.line, rejected.flowing], ['dotted', false]);
  for (const kind of ['communication', 'delegation', 'handoff', 'review', 'ownership']) {
    assert.equal(edgeStyle(edge(kind, 'a', 'b')).kind, kind);
    assert.match(edgeStyle(edge(kind, 'a', 'b')).label, / · accepted$/);
  }
  assert.equal(edgeStyle(edge('weird', 'a', 'b')).kind, 'communication');
});

test('scene edges carry geometry, ids, labels; parallel edges bend differently', () => {
  const s = layoutScene(SNAP);
  assert.equal(s.edges.length, 4);
  const ab = s.edges.filter((e) => [e.from, e.to].sort().join() === 's1,s3');
  assert.equal(ab.length, 2);
  assert.notEqual(ab[0].d, ab[1].d);
  for (const e of s.edges) {
    assert.match(e.d, /^M[\d.-]+ [\d.-]+ Q[\d.-]+ [\d.-]+ [\d.-]+ [\d.-]+$/);
    assert.ok(e.label.includes(' · '));
    assert.ok(Number.isFinite(e.mid.x));
    assert.equal(e.id, edgeId(e.raw));
  }
  assert.match(describeEdge(s.edges.find((e) => e.kind === 'delegation'), s), /Delegation from s1 to s2, planned \(planned, dashed\), task t1/);
});

test('modes dim edges instead of dropping them', () => {
  assert.deepEqual(modeKinds('communication'), ['communication']);
  const s = layoutScene(SNAP, { mode: 'communication' });
  assert.equal(s.edges.length, 4);
  assert.deepEqual(s.edges.filter((e) => !e.dimmed).map((e) => e.kind), ['communication', 'communication']);
  assert.ok(layoutScene(SNAP, { mode: 'activity' }).edges.every((e) => !e.dimmed));
  assert.ok(layoutScene(SNAP, { mode: 'replay' }).edges.every((e) => !e.dimmed));
});

test('roles come from tasks, so ownership is never inferred from delegation', () => {
  const s = layoutScene(SNAP);
  const roles = (id) => s.nodes.find((n) => n.id === id).roles.map((r) => r.role);
  assert.deepEqual(roles('s1'), ['owner']);
  assert.deepEqual(roles('s2'), ['contributor']);
  assert.deepEqual(roles('s3'), ['reviewer']);
});

test('filters: room, task, host, provider, model, kind', () => {
  const opts = filterOptions(SNAP);
  assert.deepEqual(opts.room, ['r1', 'r2']);
  assert.deepEqual(opts.host, ['h1', 'h2']);
  assert.deepEqual(opts.provider, ['local']);
  assert.deepEqual(opts.model, ['opus', 'qwen']);
  assert.deepEqual(opts.kind, ['communication', 'delegation', 'ownership']);
  assert.deepEqual(filterSnapshot(SNAP, { room: 'r1' }).agents.map((a) => a.node_id).sort(), ['s1', 's3']);
  assert.deepEqual(filterSnapshot(SNAP, { host: 'h2' }).agents.map((a) => a.node_id), ['s3']);
  assert.deepEqual(filterSnapshot(SNAP, { model: 'qwen' }).edges, []);
  const task = filterSnapshot(SNAP, { task: 't1' });
  assert.deepEqual(task.agents.map((a) => a.node_id).sort(), ['s1', 's2', 's3']);
  assert.deepEqual(task.edges.map((e) => e.kind).sort(), ['delegation', 'ownership']);
  assert.deepEqual(filterSnapshot(SNAP, { kind: 'communication' }).edges.map((e) => e.kind), ['communication', 'communication']);
  assert.equal(SNAP.agents.length, 4, 'input untouched');
});

test('a model change keeps the same node (ring pulse), a new session is a new node', () => {
  const before = { agents: [agent('s1', { model: 'm1', model_changes: [{ to: 'm1' }] })], edges: [] };
  const switched = { agents: [agent('s1', { model: 'm2', model_changes: [{ to: 'm1' }, { to: 'm2' }] })], edges: [] };
  const d = diffSnapshots(before, switched);
  assert.deepEqual([d.modelChanged, d.added, d.removed], [['s1'], [], []]);
  const fresh = { agents: [...switched.agents, agent('s2', { parent_session_id: 's1', model: 'm2' })], edges: [] };
  const d2 = diffSnapshots(switched, fresh);
  assert.deepEqual([d2.added, d2.modelChanged], [['s2'], []]);
  assert.deepEqual(layoutScene(switched).nodes.map((n) => n.id), ['s1']);
  assert.equal(layoutScene(switched).nodes[0].modelChanges, 2);
});

test('diff reports state changes and edge arrivals/transitions', () => {
  const a = { agents: [agent('s1', { state: 'idle' })], edges: [edge('delegation', 's1', 's2', 'planned')] };
  const b = { agents: [agent('s1', { state: 'working' })], edges: [edge('delegation', 's1', 's2', 'accepted'), edge('review', 's1', 's2')] };
  const d = diffSnapshots(a, b);
  assert.deepEqual(d.stateChanged, ['s1']);
  assert.equal(d.edgesChanged.length, 1);
  assert.equal(d.edgesAdded.length, 1);
});

test('alt table lists every node and edge in words', () => {
  const t = altTable(layoutScene(SNAP));
  assert.equal(t.nodes.length, 5);
  assert.equal(t.edges.length, 4);
  assert.equal(t.nodes.find((n) => n.name === 's2').context, 'ctx unknown');
  assert.ok(t.edges.some((e) => e.line === 'dashed' && e.kind === 'Delegation'));
});

test('keyboard neighbour picks the nearest node in the arrow direction', () => {
  const s = layoutScene({ agents: [agent('a'), agent('b'), agent('c')], edges: [], rooms: [], tasks: [] });
  for (const n of s.nodes) for (const dir of ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown']) {
    const t = neighbour(s, n.id, dir);
    if (t) { const o = s.nodes.find((x) => x.id === t); const v = { ArrowLeft: o.x < n.x, ArrowRight: o.x > n.x, ArrowUp: o.y < n.y, ArrowDown: o.y > n.y }[dir]; assert.ok(v); }
  }
  assert.equal(neighbour(s, 'missing', 'ArrowLeft'), s.nodes[0].id);
});

// ---- replay -------------------------------------------------------------------------------------------------------------------
const stampOf = (n) => `2026-11-12T10:00:${String(n).padStart(2, '0')}.000000Z`;
// `cursor` is the server's opaque value (ts~source~agent~actor~event_id); clients must use it as-is.
const ev = (n, type = 'tool.started', extra = {}) => ({ event_id: `e${n}`, event_type: type, ingested_at: stampOf(n), cursor: `${stampOf(n)}~pi~a1~~e${n}`, agent_id: 'a1', ...extra });

test('timeline is ordered by cursor, de-duplicated, and extendable', () => {
  const t = buildTimeline([ev(3), ev(1), ev(2), ev(2), { event_id: 'x' }, { event_id: 'y', ingested_at: stampOf(9) }]);   // no server cursor: never invented
  assert.deepEqual(t.map((e) => e.id), ['e1', 'e2', 'e3']);
  assert.equal(t[0].cursor, '2026-11-12T10:00:01.000000Z~pi~a1~~e1');   // the server's value, verbatim
  const more = buildTimeline([ev(4), ev(3)], t);
  assert.deepEqual(more.map((e) => e.id), ['e1', 'e2', 'e3', 'e4']);
  assert.equal(cursorTime(t[1].cursor) - cursorTime(t[0].cursor), 1000);
});

test('scrubbing addresses the server cursor even when event ids do not sort like arrival order', () => {
  const rows = [['e1', 1], ['e2', 2], ['z3', 3]].map(([id, n]) => ev(n, 'tool.started', { event_id: id, cursor: `${stampOf(n)}~pi~a${n}~~${id}` }));
  const t = buildTimeline(rows);
  assert.deepEqual(t.map((e) => e.cursor), rows.map((r) => r.cursor));
  assert.deepEqual([0, 1, 2].map((i) => replayFrame(t, i).cursor), rows.map((r) => r.cursor));
});

test('replay frame: null index is Live, an index addresses a cursor, out of range clamps', () => {
  const t = buildTimeline([ev(1), ev(2), ev(3)]);
  assert.equal(replayFrame(t, null).live, true);
  assert.equal(replayFrame(t, null).cursor, null);
  const f = replayFrame(t, 1);
  assert.deepEqual([f.live, f.cursor, f.fraction, f.count], [false, t[1].cursor, 0.5, 3]);
  assert.equal(replayFrame(t, 99).index, 2);
  assert.equal(replayFrame(t, -4).index, 0);
  assert.equal(replayFrame([], 0).live, true);
});

test('scrubber maps fractions to indices and playing advances to Live', () => {
  const t = buildTimeline([ev(1), ev(2), ev(3), ev(4), ev(5)]);
  assert.deepEqual([0, 0.5, 1].map((f) => indexAtFraction(t, f)), [0, 2, 4]);
  assert.equal(indexAtFraction([], 0.5), null);
  assert.equal(nextIndex(t, 1), 2);
  assert.equal(nextIndex(t, 4), null);
  assert.equal(nextIndex(t, null), 0);
});

test('step delay scales with speed and is bounded', () => {
  const t = buildTimeline([ev(1), ev(3), { ...ev(4), ingested_at: '2026-11-12T11:00:00.000000Z', cursor: '2026-11-12T11:00:00.000000Z~pi~a1~~e4' }]);
  assert.equal(stepDelay(t, 0, 1), 1500);
  assert.equal(stepDelay(t, 0, 2), 750);
  assert.equal(stepDelay(t, 1, 1), 1500, 'an hour gap is capped');
  assert.equal(stepDelay(t, 2, 1), 600, 'no next event: default');
  assert.equal(stepDelay([ev(1)].map((e) => ({ at: 0, cursor: 'a', ...e })), 0, 99), 600, 'unknown speed falls back to 1x');
  const quick = buildTimeline([ev(1), { ...ev(1), event_id: 'e1b', cursor: `${stampOf(1)}~pi~a1~~e1b` }]);
  assert.equal(stepDelay(quick, 0, 8), 15);
});

test('edge labels: parallel edges between close nodes get separate label positions', () => {
  const agents = ['a', 'b'].map((id) => ({ node_id: id, agent_id: id, model: 'm', state: 'working', room_ids: [], context: null }));
  const edge = (kind, n) => ({ kind, from: 'a', to: 'b', state: 'sent', room_id: null, task_id: `t${n}`, event_id: `e${n}`, provenance: 'observed', updated_at: '2026-11-12T10:00:00Z' });
  const scene = layoutScene({ agents, rooms: [], tasks: [], edges: [edge('communication', 1), edge('delegation', 2), edge('handoff', 3)] }, {});
  const spots = scene.edges.map((e) => `${e.labelAt.x},${e.labelAt.y}`);
  assert.equal(new Set(spots).size, 3);
  assert.ok(scene.edges.every((e) => e.at === undefined && Number.isFinite(e.labelAt.x)));
});

// ---- S8 ring buffer, health, hosts, drift, stalled handoffs ------------------------------------------------------------------
test('timeline is a ring buffer: oldest entries drop at the cap, newest always survive', () => {
  assert.equal(TIMELINE_CAP, 2000);
  const evs = Array.from({ length: 12 }, (_, i) => ev(i + 1));
  const t = buildTimeline(evs, [], 5);
  assert.deepEqual(t.map((e) => e.id), ['e8', 'e9', 'e10', 'e11', 'e12']);
  const more = buildTimeline([ev(13)], t, 5);
  assert.deepEqual(more.map((e) => e.id), ['e9', 'e10', 'e11', 'e12', 'e13']);
  assert.equal(buildTimeline(Array.from({ length: 2100 }, (_, i) => ({ ...ev(1), event_id: `x${String(i).padStart(4, '0')}`, cursor: `${stampOf(1)}~pi~a1~~x${String(i).padStart(4, '0')}` }))).length, 2000);
});

test('context health: backend measured fraction feeds the badge, unknown stays unknown and never 0', () => {
  const measured = layoutScene({ agents: [agent('h', { context: null, context_health: { fraction: 0.92, level: 'high', measured: true } })], edges: [], rooms: [], tasks: [] }).nodes[0];
  assert.equal(measured.ctx.percent, 92); assert.equal(measured.ctx.level, 'critical');
  const unknown = layoutScene({ agents: [agent('u', { context: null, context_health: { fraction: null, level: 'unknown', measured: false } })], edges: [], rooms: [], tasks: [] }).nodes[0];
  assert.equal(unknown.ctx.label, 'ctx unknown'); assert.equal(unknown.ctx.percent, null);
  assert.equal(contextInfo({ used: 70, limit: 100 }).level, 'warn'); assert.equal(contextInfo({ used: 90, limit: 100 }).level, 'critical');
});

test('host strip: id, agent count, connection state; missing state says unknown', () => {
  const rows = hostStrip({ hosts: [
    { host_id: 'h1', agents: ['a', 'b'], state: 'connected', connected: true, last_seen: 't', reconnects: 1, context_health: { measured_count: 2, total: 2, average_fraction: 0.75, level: 'elevated' } },
    { host_id: 'h2', agents: ['c'], connected: null }, { host_id: 'h3', agents: [], state: 'disconnected' }] });
  assert.deepEqual(rows.map((r) => [r.id, r.agents, r.state]), [['h1', 2, 'connected'], ['h2', 1, 'unknown'], ['h3', 0, 'disconnected']]);
  assert.equal(rows[0].ctx.percent, 75); assert.equal(rows[0].ctx.level, 'warn'); assert.equal(rows[1].ctx.label, 'ctx unknown');
  assert.deepEqual(hostStrip({}), []);
});

test('drift items are labelled heuristic with provenance and evidence; absent drift is empty', () => {
  const items = driftItems({ drift: [{ kind: 'handoff_stalled', provenance: 'derived', evidence_event_ids: ['e5', 'e3'], task_id: 't1', heuristic: true, note: 'n' }, { kind: 'weird_kind', provenance: 'inferred' }] });
  assert.equal(items[0].title, 'Handoff stalled'); assert.equal(items[0].provenance, 'derived'); assert.deepEqual(items[0].evidence, ['e5', 'e3']);
  assert.equal(items[1].title, 'Weird kind'); assert.equal(items[1].provenance, 'inferred');
  assert.deepEqual(driftItems({}), []); assert.deepEqual(driftItems(null), []);
  const n = layoutScene({ agents: [agent('d', { drift: [{ kind: 'repeated_tool_failures', provenance: 'derived' }] })], edges: [], rooms: [], tasks: [] }).nodes[0];
  assert.equal(n.driftItems.length, 1);
});

test('handoff edge states stalled and failed have distinct styles and text', () => {
  const stalled = edgeStyle({ kind: 'handoff', state: 'stalled' }), failed = edgeStyle({ kind: 'handoff', state: 'failed' });
  assert.equal(stalled.line, 'stalled'); assert.ok(stalled.dash); assert.equal(stalled.flowing, false); assert.match(stalled.label, /stalled/);
  assert.equal(failed.line, 'dotted'); assert.equal(failed.flowing, false);
});

// ---- visual pass: fit, person in room, labels, names, markers -----------------------------------------------------------------
const ROOM = (over = {}) => ({
  agents: ['c', 'o', 'q', 'g'].map((id) => agent(id, { room_ids: ['rX'], label: id })),
  rooms: [{ room_id: 'rX', name: 'product-dev', agent_ids: ['c', 'o', 'q', 'g'] }],
  edges: [edge('message' === 'x' ? 'x' : 'communication', 'user:alice', 'c', 'sent', { room_id: 'rX' }), edge('handoff', 'o', 'q', 'completed', { room_id: 'rX', handoff_id: 'h1' }),
    edge('handoff', 'q', 'g', 'stalled', { room_id: 'rX', handoff_id: 'h2' }), edge('delegation', 'c', 'q', 'planned', { room_id: 'rX', task_id: 't9' }), edge('ownership', 'c', 'q', 'transferred', { room_id: 'rX', task_id: 't9' })],
  tasks: [], cursor: null, ...over,
});

test('a person who takes part in a room sits inside that room, not in a No-room cluster', () => {
  const s = layoutScene(ROOM());
  const alice = s.nodes.find((n) => n.id === 'user:alice');
  assert.equal(alice.room, 'rX');
  assert.deepEqual(s.rooms.map((r) => r.id), ['rX']);
  const r = s.rooms[0];
  assert.ok(Math.hypot((alice.x - r.cx) / r.rx, (alice.y - r.cy) / r.ry) <= 1, 'inside the room ellipse');
});

test('the Room filter drops a person whose edges belong to another room', () => {
  const snap = ROOM({ agents: [...ROOM().agents, agent('z', { room_ids: ['rY'] })], rooms: [...ROOM().rooms, { room_id: 'rY', agent_ids: ['z'] }],
    edges: [...ROOM().edges, edge('communication', 'user:bob', 'z', 'sent', { room_id: 'rY' })] });
  const f = layoutScene(snap, { filters: { room: 'rX' } });
  assert.deepEqual(f.nodes.map((n) => n.id).sort(), ['c', 'g', 'o', 'q', 'user:alice']);
  assert.ok(f.nodes.every((n) => n.room === 'rX'));
  assert.ok(!layoutScene(snap, { filters: { room: 'rY' } }).nodes.some((n) => n.id === 'user:alice'));
});

test('the scene is fitted: the viewBox hugs the content and follows the requested aspect', () => {
  const wide = layoutScene(ROOM(), { aspect: 1.5 }), tall = layoutScene(ROOM(), { aspect: 0.6 });
  assert.ok(wide.viewBox.w / wide.viewBox.h > tall.viewBox.w / tall.viewBox.h);
  assert.ok(tall.viewBox.w < wide.viewBox.w * 0.6, 'a phone scene is much narrower, so it scales up to fit the card');
  assert.equal(tall.rooms[0].shape, 'rect'); assert.equal(wide.rooms[0].shape, 'ellipse');
  assert.ok(tall.nodes.every((n) => Math.abs(n.x - tall.rooms[0].cx) <= tall.rooms[0].rx - HALO_R + 1), 'phone nodes sit inside the box');
  assert.ok(tall.compact && !wide.compact);
  for (const s of [wide, tall]) for (const n of s.nodes) {
    const vb = s.viewBox;
    assert.ok(n.x - RING_R >= vb.x && n.x + RING_R <= vb.x + vb.w && n.y - RING_R >= vb.y && n.y + RING_R + 40 <= vb.y + vb.h, `${n.id} fits`);
  }
  const one = layoutScene({ agents: [agent('solo')], rooms: [], edges: [], tasks: [] });
  assert.ok(one.viewBox.w >= 520, 'a single node is not blown up to fill the canvas');
});

test('edge labels never overlap each other: a label with no free spot goes quiet instead', () => {
  const s = layoutScene(ROOM());
  const shown = s.edges.filter((e) => !e.labelQuiet);
  assert.ok(shown.length >= 1);
  for (const a of shown) for (const b of shown) if (a.id < b.id) {
    const x = Math.abs(a.labelBox.x - b.labelBox.x) < (a.labelBox.w + b.labelBox.w) / 2, y = Math.abs(a.labelBox.y - b.labelBox.y) < (a.labelBox.h + b.labelBox.h) / 2;
    assert.ok(!(x && y), `${a.id} / ${b.id} overlap`);
  }
});

test('room title is the room name; unknown name falls back to a shortened id', () => {
  assert.equal(layoutScene(ROOM()).rooms[0].label, 'product-dev');
  const noName = layoutScene(ROOM({ rooms: [{ room_id: 'room_0123456789ABCDEFRZGBJ', name: null, agent_ids: [] }], agents: ROOM().agents.map((a) => ({ ...a, room_ids: ['room_0123456789ABCDEFRZGBJ'] })) }));
  assert.match(noName.rooms[0].label, /^Room room_0….*ZGBJ$/);
  assert.deepEqual(filterOptions(ROOM()).roomName, { rX: 'product-dev' });
});

test('drift and model switches are node signals of their own, distinct from handoff edges', () => {
  const snap = ROOM({ agents: [agent('c', { room_ids: ['rX'], drift: [{ kind: 'reported_drift_warning', provenance: 'derived', evidence_event_ids: [] }], model: 'opus', model_changes: [{ from: 'sonnet', to: 'opus', at: 't' }] }),
    agent('o', { room_ids: ['rX'] })], rooms: [{ room_id: 'rX', agent_ids: ['c', 'o'] }], edges: [] });
  const [c, o] = layoutScene(snap).nodes;
  assert.equal(c.hasDrift, true); assert.equal(c.switchedFrom, 'sonnet'); assert.equal(o.hasDrift, false); assert.equal(o.switchedFrom, null);
  assert.match(describeNode(c), /drift signal \(heuristic\)/); assert.match(describeNode(c), /model switched from sonnet/);
});

const denseGraph = (agents, edges) => ({
  ...SNAP, rooms: [], tasks: [],
  agents: Array.from({ length: agents }, (_, i) => agent(`d${String(i).padStart(2, '0')}`, { room_ids: [`r${i % 4}`] })),
  edges: Array.from({ length: edges }, (_, i) => {
    const from = (i * 7) % agents, to = (from + 1 + ((i * 13) % (agents - 1))) % agents;
    return edge(i % 2 ? 'communication' : 'delegation', `d${String(from).padStart(2, '0')}`, `d${String(to).padStart(2, '0')}`, 'accepted', { event_id: `ev${i}`, label: `msg ${i}` });
  }),
});

// Catastrophic-regression guard, not a benchmark: the limit is deliberately loose (best of 5 under 2000 ms).
test('layoutScene catastrophic-regression guard: best of 5 under 2000 ms on a maxed-out graph (60 agents, 2000 edges)', () => {
  const snap = denseGraph(60, 2000);
  layoutScene(snap);   // warm-up: the first call pays for JIT
  const runs = [];
  for (let run = 0; run < 5; run += 1) {
    const t0 = performance.now();
    layoutScene(snap);
    runs.push(performance.now() - t0);
  }
  // Best of five against 2s: the quadratic placer took 13-16s, and a loaded CI runner only slows each run.
  const best = Math.min(...runs);
  assert.ok(best < 2000, `layoutScene best run took ${best.toFixed(0)}ms (runs ${runs.map((r) => r.toFixed(0)).join(', ')})`);
});

test('estimateTextWidth counts CJK and emoji as full-width glyphs', () => {
  assert.equal(estimateTextWidth('漢', 10), 10);
  assert.equal(estimateTextWidth('🚀', 10), 10);
  assert.equal(estimateTextWidth('a', 10), 5.8);
});
