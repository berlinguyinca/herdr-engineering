import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { HerdrClient } from './herdr-web-client/client.js';
import { layoutScene, buildTimeline } from './agent-ops-model.js';
import { API, loadSnapshot, loadReplay, loadTimeline, streamUrl, renderTabs, renderFilters, renderScene, renderNode, renderEdge, renderInspector, renderTimeline, renderBanner, renderAltTable, renderEmpty, renderLegend } from './agent-ops-view.js';

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
const EVENTS = [1, 2, 3].map((n) => ({ event_id: `e${n}`, ingested_at: `2026-11-12T10:00:0${n}.000000Z`, event_type: 'tool.started', agent_id: 'a1', session_id: null }));

// A real HTTP server speaking the agent-ops routes' contract (404 while off), and the real client.
function serve({ enabled = true, fail = false } = {}) {
  const seen = [];
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://x');
    seen.push(url.pathname + url.search);
    const send = (code, body) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(body)); };
    if (!enabled || !url.pathname.startsWith(API)) return send(404, { error: 'not found' });
    if (fail) return send(500, { error: 'boom' });
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
