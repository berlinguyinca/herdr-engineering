import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { HerdrClient } from './herdr-web-client/client.js';
import { layoutScene, buildTimeline } from './agent-ops-model.js';
import { API, loadSnapshot, loadReplay, loadTimeline, streamUrl, renderTabs, renderFilters, renderScene, renderNode, renderEdge, renderInspector, renderTimeline, renderBanner, renderAltTable, renderEmpty, renderLegend, renderHostStrip, renderDrift, sceneSignature } from './agent-ops-view.js';

const agent = (id, over = {}) => ({ node_id: id, agent_id: id, session_id: null, host_id: 'h1', provider: null, model: 'opus', model_changes: [], state: 'working', context: { used: 50, limit: 100, source: 'pi' },
  room_ids: [], parent_session_id: null, compactions: 0, drift_warnings: 0, label: null, provenance: 'observed', last_event_at: '2026-11-12T10:00:00.000000Z', ...over });
const SNAP = {
  agents: [agent('a1', { label: '<b>Planner</b>' }), agent('a2', { context: null, model: null, state: 'idle' }), agent('a3', { context: { used: 95, limit: 100 } })],
  rooms: [], edges: [
    { kind: 'delegation', from: 'a1', to: 'a2', state: 'planned', room_id: null, event_id: 'e2', provenance: 'observed', task_id: 't1', handoff_id: null, updated_at: '2026-11-12T10:00:02.000000Z' },
    { kind: 'communication', from: 'a1', to: 'a3', state: 'sent', room_id: null, event_id: 'e3', provenance: 'observed', task_id: null, handoff_id: null, updated_at: '2026-11-12T10:00:03.000000Z' }],
  tasks: [{ task_id: 't1', room_id: null, accountable_owner: 'a1', contributors: ['a2'], reviewer: 'a3', updated_at: '2026-11-12T10:00:02.000000Z' }],
  cursor: '2026-11-12T10:00:03.000000Z~e3', counts: {}, truncated: false,
};
const EVENTS = [1, 2, 3].map((n) => ({ event_id: `e${n}`, ingested_at: `2026-11-12T10:00:0${n}.000000Z`, cursor: `2026-11-12T10:00:0${n}.000000Z~pi~a1~~e${n}`, event_type: 'tool.started', agent_id: 'a1', session_id: null }));

// A real HTTP server speaking the agent-ops routes' contract (404 while off), and the real client.
function serve({ enabled = true, fail = false, badCursor = false } = {}) {
  const seen = [];
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://x');
    seen.push(url.pathname + url.search);
    const send = (code, body) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(body)); };
    if (!enabled || !url.pathname.startsWith(API)) return send(404, { error: 'not found' });
    if (fail) return send(500, { error: 'boom' });
    if (badCursor && !url.pathname.endsWith('/snapshot')) return send(400, { error: 'invalid cursor' });
    if (url.pathname.endsWith('/snapshot')) return send(200, SNAP);
    if (url.pathname.endsWith('/replay')) return send(200, { ...SNAP, edges: [], cursor: url.searchParams.get('until') });
    if (url.pathname.endsWith('/events')) {
      const after = url.searchParams.get('cursor');
      return after ? send(200, { events: EVENTS.slice(2), next_cursor: null, has_more: false }) : send(200, { events: EVENTS.slice(0, 2), next_cursor: 'c2', has_more: true });
    }
    return send(404, {});
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve({ client: new HerdrClient(`http://127.0.0.1:${server.address().port}`), seen, close: () => server.close() })));
}

test('loaders: snapshot, replay with the cursor, paginated timeline; real client + real server', async () => {
  const s = await serve();
  try {
    const snap = await loadSnapshot(s.client);
    assert.equal(snap.status, 'ok'); assert.equal(snap.body.agents.length, 3);
    const rep = await loadReplay(s.client, '2026-11-12T10:00:02.000000Z~e2');
    assert.equal(rep.body.cursor, '2026-11-12T10:00:02.000000Z~e2');
    assert.ok(s.seen.some((p) => p === `${API}/replay?until=${encodeURIComponent('2026-11-12T10:00:02.000000Z~e2')}`));
    const tl = await loadTimeline(s.client);
    assert.deepEqual(tl.body.events.map((e) => e.event_id), ['e1', 'e2', 'e3']);
  } finally { s.close(); }
});

test('capability off is "off" (a 404 is an answer), a server error is "error"', async () => {
  const off = await serve({ enabled: false });
  const bad = await serve({ fail: true });
  try {
    assert.equal((await loadSnapshot(off.client)).status, 'off');
    const e = await loadSnapshot(bad.client);
    assert.equal(e.status, 'error'); assert.ok(e.message);
  } finally { off.close(); bad.close(); }
});

test('a 400 on a cursor is a reset (re-fetch the snapshot), a 500 is not', async () => {
  const s = await serve({ badCursor: true });
  const bad = await serve({ fail: true });
  try {
    const r = await loadReplay(s.client, 'old~format');
    assert.equal(r.status, 'error'); assert.equal(r.reset, true);
    assert.equal((await loadSnapshot(s.client)).status, 'ok');
    assert.equal((await loadReplay(bad.client, 'x')).reset, undefined);
  } finally { s.close(); bad.close(); }
});

test('stream url resumes from the cursor', () => {
  assert.equal(streamUrl('', ''), `${API}/stream`);
  assert.equal(streamUrl('http://x', 'a~b'), `http://x${API}/stream?cursor=a~b`);
});

const scene = layoutScene(SNAP);

test('tabs and filters: five modes, six filters, selected state is exposed', () => {
  const t = renderTabs('handoffs');
  for (const m of ['Activity', 'Communication', 'Handoffs', 'Ownership', 'Replay']) assert.match(t, new RegExp(`>${m}<`));
  assert.match(t, /data-ao-mode="handoffs"[^>]*aria-selected="true"/);
  const f = renderFilters({ room: [], task: ['t1'], host: ['h1'], provider: [], model: ['opus'], kind: ['delegation'] }, { task: 't1' });
  assert.equal((f.match(/data-ao-filter=/g) || []).length, 6);
  assert.match(f, /<option value="t1" selected>/);
});

test('node: model inside, ctx ring text, unknown is explicit and never 0%, no markup injection', () => {
  const html = renderScene(scene);
  assert.match(html, /ctx unknown/);
  assert.doesNotMatch(html, /\b0% ctx/);
  assert.match(html, /model unknown/);
  assert.doesNotMatch(html, /<b>Planner<\/b>/);
  assert.match(html, /&lt;b&gt;Planner/);
  assert.match(html, /95% ctx/); assert.match(html, /ctx critical/);
  const n = scene.nodes.find((x) => x.id === 'a2');
  assert.match(renderNode(n), /ao-ring-unknown/);
});

test('edges: dashed when planned, solid when accepted, always a text label and a marker, particles only when flowing', () => {
  const planned = scene.edges.find((e) => e.kind === 'delegation');
  const h = renderEdge(planned, scene);
  assert.match(h, /stroke-dasharray="9 7"/); assert.match(h, /Delegation · planned/); assert.match(h, /ao-particle/);
  assert.doesNotMatch(renderEdge(planned, scene, { flowing: false }), /ao-particle/);
  assert.doesNotMatch(renderEdge(scene.edges.find((e) => e.kind === 'communication'), scene), /stroke-dasharray/);
});

test('inspector: node, edge (metadata only), task with owner/contributors/reviewer', () => {
  const node = renderInspector({ type: 'node', id: 'a1' }, scene, SNAP);
  assert.match(node, /Open pane/); assert.match(node, /task t1|data-ao-task="t1"/);
  const edge = renderInspector({ type: 'edge', id: scene.edges[0].id }, scene, SNAP);
  assert.match(edge, /Metadata only/); assert.doesNotMatch(edge, /message text/i);
  const task = renderInspector({ type: 'task', id: 't1' }, scene, SNAP);
  assert.match(task, /Accountable owner/); assert.match(task, /Contributors/); assert.match(task, /Reviewer/);
  assert.match(renderInspector({ type: 'node', id: 'gone' }, scene, SNAP), /not in the current view/);
});

test('timeline: Live vs scrubbed, speed list, scrubber label, banner states', () => {
  const timeline = buildTimeline(EVENTS);
  const live = renderTimeline({ timeline, index: null, playing: false, speed: 1 });
  assert.match(live, /data-ao-live aria-pressed="true"/); assert.match(live, /max="2" value="2"/);
  const at = renderTimeline({ timeline, index: 0, playing: true, speed: 4 });
  assert.match(at, /Pause/); assert.match(at, /<option value="4" selected>/); assert.match(at, /1 \/ 3/);
  assert.match(renderTimeline({ timeline: [], index: null, playing: false, speed: 1 }), /No events yet/);
  assert.equal(renderBanner('live'), ''); assert.match(renderBanner('offline'), /paused/);
});

test('text alternative lists every node and edge; empty states distinguish filters from no data', () => {
  const alt = renderAltTable(scene);
  assert.equal((alt.match(/<tbody>/g) || []).length, 2);
  assert.match(alt, /3 agents, 2 relationships/); assert.match(alt, /ctx unknown/);
  assert.match(renderEmpty(SNAP, true), /Nothing matches/); assert.match(renderEmpty(SNAP, false), /No agent activity yet/);
  assert.match(renderLegend(), /planned \(dashed\)/);
});

// ---- S8/S9/N7 and health/drift display ----------------------------------------------------------------------------------------------
function serveHistory(total, pageSize) {
  const all = Array.from({ length: total }, (_, i) => ({ event_id: `e${String(i + 1).padStart(4, '0')}`, ingested_at: `2026-11-12T10:00:00.${String(i + 1).padStart(6, '0')}Z`, cursor: `2026-11-12T10:00:00.${String(i + 1).padStart(6, '0')}Z~pi~a1~~e${String(i + 1).padStart(4, '0')}`, event_type: 'tool.started', agent_id: 'a1' }));
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://x'), after = url.searchParams.get('cursor');
    const rest = after ? all.filter((e) => e.cursor > after) : all, page = rest.slice(0, pageSize);
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ events: page, next_cursor: page.length ? page.at(-1).cursor : after, has_more: rest.length > pageSize }));
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve({ client: new HerdrClient(`http://127.0.0.1:${server.address().port}`), close: () => server.close() })));
}

test('timeline loads the tail of a long history, bounded by the cap', async () => {
  const s = await serveHistory(50, 10);
  try {
    const tl = await loadTimeline(s.client, { cap: 15 });
    assert.equal(tl.body.events.length, 15);
    assert.equal(tl.body.events.at(-1).event_id, 'e0050'); assert.equal(tl.body.events[0].event_id, 'e0036');
  } finally { s.close(); }
});

test('roving tabindex: exactly one node is a tab stop, the chosen one', () => {
  const two = layoutScene(SNAP);
  const html = renderScene(two, { tabId: 'a3' });
  const stops = [...html.matchAll(/data-ao-node="([^"]+)"[^>]*tabindex="(-?\d)"/g)].map((m) => [m[1], m[2]]);
  assert.deepEqual(stops.filter(([, t]) => t === '0').map(([id]) => id), ['a3']);
  assert.equal(stops.filter(([, t]) => t === '-1').length, 2);
  assert.equal([...renderScene(two).matchAll(/data-ao-node="[^"]+"[^>]*tabindex="0"/g)].length, 1, 'defaults to the first node');
});

test('inspector live region and the per-event caption are not chatty', () => {
  assert.doesNotMatch(renderTimeline({ timeline: buildTimeline(EVENTS), index: null, playing: false, speed: 1 }), /role="status"/);
});

const HEALTHY = { ...SNAP,
  agents: [agent('a1', { host_id: 'h1', drift: [{ kind: 'handoff_stalled', provenance: 'derived', evidence_event_ids: ['e5', 'e3'], task_id: 't1', heuristic: true, note: 'x' }] }), agent('a2', { host_id: 'h2', context: null })],
  edges: [{ kind: 'handoff', from: 'a1', to: 'a2', state: 'stalled', room_id: null, event_id: 'e9', provenance: 'observed', task_id: 't1', handoff_id: 'ho1', interrupted_by: 'e8', updated_at: 't' },
    { kind: 'handoff', from: 'a2', to: 'a1', state: 'failed', room_id: null, event_id: 'e10', provenance: 'observed', task_id: null, handoff_id: 'ho2', updated_at: 't' }],
  tasks: [{ ...SNAP.tasks[0], drift: [{ kind: 'handoff_stalled', provenance: 'derived', evidence_event_ids: ['e5'], task_id: 't1', heuristic: true, note: 'x' }] }],
  hosts: [{ host_id: 'h1', agents: ['a1'], state: 'connected', connected: true, reconnects: 0, context_health: { measured_count: 1, total: 1, average_fraction: 0.5, level: 'ok' } },
    { host_id: 'h2', agents: ['a2'], state: 'disconnected', connected: false, reconnects: 1, context_health: { measured_count: 0, total: 1, average_fraction: null, level: 'unknown' } },
    { host_id: 'h3', agents: [], connected: null }],
  health: { context: { measured_count: 1, total: 2, average_fraction: 0.5, level: 'ok' } }, counts: { hosts: 3 } };

test('host strip: host id, agent count, state words; unknown says unknown; ctx unknown never 0', () => {
  const html = renderHostStrip(HEALTHY);
  assert.match(html, /h1/); assert.match(html, /1 agent\b/); assert.match(html, /connected/); assert.match(html, /disconnected/);
  assert.match(html, /h3[\s\S]*0 agents[\s\S]*unknown/); assert.match(html, /ctx unknown/); assert.doesNotMatch(html, /\b0% ctx/);
  assert.match(html, /50% ctx/);
  assert.equal(renderHostStrip({ agents: [] }), '');
});

test('drift is labelled heuristic with provenance and evidence, in node and task inspectors', () => {
  const sc = layoutScene(HEALTHY);
  const node = renderInspector({ type: 'node', id: 'a1' }, sc, HEALTHY);
  assert.match(node, /Handoff stalled/); assert.match(node, /heuristic/i); assert.match(node, /derived/); assert.match(node, /e5/); assert.match(node, /not proof/i);
  const task = renderInspector({ type: 'task', id: 't1' }, sc, HEALTHY);
  assert.match(task, /Handoff stalled/); assert.match(task, /heuristic/i);
  assert.equal(renderDrift([]), '');
  assert.doesNotMatch(renderInspector({ type: 'node', id: 'a2' }, sc, HEALTHY), /Drift signals/);
});

test('stalled and failed handoff edges: distinct line, state in text, explained in the inspector', () => {
  const sc = layoutScene(HEALTHY);
  const stalled = sc.edges.find((e) => e.state === 'stalled'), failed = sc.edges.find((e) => e.state === 'failed');
  const sh = renderEdge(stalled, sc);
  assert.match(sh, /Handoff · stalled/); assert.match(sh, /ao-edge--stalled/); assert.doesNotMatch(sh, /ao-particle/);
  assert.match(renderEdge(failed, sc), /Handoff · failed/);
  assert.match(renderInspector({ type: 'edge', id: stalled.id }, sc, HEALTHY), /stalled[\s\S]*Interrupted by[\s\S]*e8/);
  assert.match(renderInspector({ type: 'edge', id: failed.id }, sc, HEALTHY), /failed/);
  assert.match(renderLegend(), /stalled/);
});

// ---- visual pass: node markers, fitted svg, room names -------------------------------------------------------------------------
test('node carries a drift badge (heuristic) and a model-switched marker, each with a text form', () => {
  const snap = { ...SNAP, agents: [agent('a1', { model: 'opus', model_changes: [{ from: 'sonnet-4', to: 'opus' }], drift: [{ kind: 'reported_drift_warning', provenance: 'derived', evidence_event_ids: [] }] }), agent('a2')], edges: [], tasks: [] };
  const [a1, a2] = layoutScene(snap).nodes;
  const html = renderNode(a1), plain = renderNode(a2);
  assert.match(html, /class="ao-drift-badge"/);
  assert.match(html, /<title>Drift signal \(heuristic\)<\/title>/);
  assert.match(html, /class="ao-switched"/);
  assert.match(html, /switched from sonnet-4/);
  assert.match(html, /aria-label="[^"]*drift signal \(heuristic\)[^"]*model switched from sonnet-4/);
  assert.ok(!/ao-drift-badge|ao-switched/.test(plain));
});

test('scene svg is fitted to its content (aspect-ratio from the viewBox), room is titled by name, compact on phones', () => {
  const snap = { ...SNAP, agents: SNAP.agents.map((a) => ({ ...a, room_ids: ['rX'] })), rooms: [{ room_id: 'rX', name: 'product-dev', agent_ids: ['a1', 'a2', 'a3'] }] };
  const wide = layoutScene(snap, { aspect: 1.4 }), phone = layoutScene(snap, { aspect: 0.55 });
  const w = renderScene(wide), p = renderScene(phone);
  assert.match(w, new RegExp(`aspect-ratio:${wide.viewBox.w} / ${wide.viewBox.h}`));
  assert.match(w, /<ellipse /);
  assert.match(w, />product-dev/);
  assert.ok(!/ao-svg--compact/.test(w)); assert.match(p, /ao-svg--compact/);
  assert.ok(!/ROOM_|Room rX/.test(w));
});

test('room filter option shows the room name', () => {
  const html = renderFilters({ room: ['rX'], roomName: { rX: 'product-dev' } }, { room: '' });
  assert.match(html, /<option value="rX">product-dev<\/option>/);
  assert.match(renderFilters({ room: ['room_0123456789ABCDEF'] }, {}), /room_0….*CDEF/);
});

test('scene signature: drift or a model switch appearing alone changes it (repaint), room shape too', () => {
  const base = layoutScene(SNAP, { aspect: 1.4 });
  const drifted = layoutScene({ ...SNAP, agents: SNAP.agents.map((a) => (a.node_id === 'a1' ? { ...a, drift_warnings: 2 } : a)) }, { aspect: 1.4 });
  const switched = layoutScene({ ...SNAP, agents: SNAP.agents.map((a) => (a.node_id === 'a1' ? { ...a, model_changes: [{ from: 'sonnet', to: 'opus' }] } : a)) }, { aspect: 1.4 });
  assert.notEqual(sceneSignature(drifted, {}), sceneSignature(base, {}));
  assert.notEqual(sceneSignature(switched, {}), sceneSignature(base, {}));
  assert.equal(sceneSignature(base, {}), sceneSignature(layoutScene(SNAP, { aspect: 1.4 }), {}));
  const phone = layoutScene({ ...SNAP, rooms: [{ room_id: 'rX', name: 'a', agent_ids: ['a1'] }], agents: SNAP.agents.map((a) => ({ ...a, room_ids: ['rX'] })) }, { aspect: 1.4 });
  const renamed = { ...phone, rooms: phone.rooms.map((r) => ({ ...r, label: 'other', shape: 'rect' })) };
  assert.notEqual(sceneSignature(renamed, {}), sceneSignature(phone, {}));
  const ctxChanged = { ...phone, rooms: phone.rooms.map((r) => ({ ...r, ctx: { ...r.ctx, percent: 55, known: true } })) };
  assert.notEqual(sceneSignature(ctxChanged, {}), sceneSignature(phone, {}));
  assert.notEqual(sceneSignature({ ...phone, truncated: true }, {}), sceneSignature({ ...phone, truncated: false }, {}));
});

test('layoutScene survives a non-finite aspect', () => {
  for (const aspect of [NaN, Infinity, undefined]) {
    const s = layoutScene(SNAP, { aspect });
    assert.deepEqual(s.nodes.map((n) => [n.x, n.y]), layoutScene(SNAP, { aspect: 1.35 }).nodes.map((n) => [n.x, n.y]));
    assert.ok(s.nodes.every((n) => Number.isFinite(n.x) && Number.isFinite(n.y)));
  }
});
