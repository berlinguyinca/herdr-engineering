import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { HerdrClient } from './herdr-web-client/client.js';
import { MAX_DRAWN, layoutToSave, loadTeamMap, moveNode, placeNodes, renderBar, renderBody, renderCanvas, renderText, rootRef, saveTeamMapLayout, viewModel } from './team-map-view.js';
import { CELL } from './team-map-model.js';

const node = (id, kind, over = {}) => ({ id, kind, display_name: id.split(':')[1], native_id: id.split(':')[1], status: 'open', tombstone: false, ...over });
const edge = (id, type, from, to, live = true) => ({ id, type, from_id: from, to_id: to, live });
const SNAP = {
  nodes: [node('project:p', 'project'), node('issue:i1', 'issue'), node('task:t1', 'task', { status: 'done' }), node('task:t2', 'task', { status: 'blocked' }),
    node('artifact:a1', 'artifact', { display_name: '<b>report</b>' }), node('agent_session:s1', 'agent_session'), node('review:r1', 'review')],
  edges: [edge('e1', 'contains', 'project:p', 'issue:i1'), edge('e2', 'contains', 'issue:i1', 'task:t1'), edge('e3', 'contains', 'issue:i1', 'task:t2'),
    edge('e4', 'produced', 'task:t1', 'artifact:a1'), edge('e5', 'reviews', 'review:r1', 'task:t1'), edge('e6', 'depends_on', 'task:t2', 'task:t1', false)],
};

// A real HTTP server speaking the Work Graph routes' contract (404 while off, ETag/If-Match on the layout), and the real client.
function serve({ enabled = true, snapshot = SNAP, layouts = {} } = {}) {
  const docs = { default: { nodes: {}, ...layouts.default }, mine: { nodes: {}, ...layouts.mine } };
  let version = 0;
  const seen = { puts: [] };
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://x');
    const send = (code, body) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(body)); };
    if (!enabled || !url.pathname.startsWith('/api/v1/graph/work/')) return send(404, { error: 'not found' });
    if (url.pathname.endsWith('/snapshot')) return url.searchParams.get('root') === 'project:p' ? send(200, snapshot) : send(404, { error: 'not found' });
    if (req.method === 'GET') { const w = url.searchParams.get('which'); return send(200, { document: docs[w === 'default' ? 'default' : 'mine'], etag: `"v${version}"` }); }
    let raw = '';
    req.on('data', (c) => { raw += c; });
    req.on('end', () => {
      const b = JSON.parse(raw);
      seen.puts.push(b);
      if (b.if_match !== `"v${version}"`) return send(412, { error: 'stale etag' });
      version += 1; docs.mine = b.document;
      return send(200, { document: b.document, etag: `"v${version}"`, version });
    });
    return undefined;
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve({
    client: new HerdrClient(`http://127.0.0.1:${server.address().port}`), seen, docs, bump: () => { version += 1; }, close: () => server.close(),
  })));
}

test('a project id without a kind is a project', () => {
  assert.equal(rootRef(' atlas '), 'project:atlas');
  assert.equal(rootRef('issue:i1'), 'issue:i1');
  assert.equal(rootRef('  '), '');
});

test('the picture draws every visible node as a keyboard-operable, labelled button with one tab stop; text is escaped', () => {
  const vm = viewModel(SNAP);
  const html = renderCanvas(vm, placeNodes(vm.nodes));
  assert.equal((html.match(/role="button"/g) || []).length, vm.nodes.length);
  assert.equal((html.match(/tabindex="0"/g) || []).length, 1, 'roving tabindex');
  assert.match(html, /aria-label="task t1, status done, 1 deliverable\. Enter shows details\./);
  assert.match(html, /aria-label="issue i1, status open, 1 of 2 tasks done/);
  assert.doesNotMatch(html, /<b>report/);
  assert.match(html, /&lt;b&gt;report/);
  assert.doesNotMatch(html, /agent_session s1/, 'execution nodes are folded away by default');
  assert.doesNotMatch(html, /%/, 'no percentages');
});

test('lines carry their meaning in shape and words, earlier relations are marked, and contains lines are not drawn', () => {
  const vm = viewModel(SNAP);
  const html = renderCanvas(vm, placeNodes(vm.nodes));
  assert.match(html, /data-edge="e4" data-meaning="produced"/);
  assert.match(html, /tm-edge--dependency tm-edge--past/);
  assert.match(html, /<title>waits for \(earlier, no longer current\)<\/title>/);
  assert.doesNotMatch(html, /data-edge="e1"/);
});

test('the written map has every node and relation and works without the picture', () => {
  const html = renderText(viewModel(SNAP));
  assert.match(html, /<summary>Written map/);
  for (const id of ['project:p', 'issue:i1', 'task:t1', 'task:t2', 'review:r1']) assert.match(html, new RegExp(`data-open-node="${id}"`));
  assert.match(html, /1 of 2 tasks done/);
  assert.match(html, /t1 produced &lt;b&gt;report&lt;\/b&gt;/);
  assert.match(html, /data-live="false">t2 waits for t1 \(earlier, no longer current\)/);
  assert.doesNotMatch(html, /works together with/, 'the hierarchy is the indentation, not a relation');
});

test('filters narrow the picture and the text together', () => {
  const vm = viewModel(SNAP, { kind: 'task', status: 'blocked' });
  assert.deepEqual(vm.nodes.map((n) => n.id), ['task:t2']);
  assert.equal(renderText(vm).includes('task:t1'), false);
  assert.ok(viewModel(SNAP, { showExecution: true }).nodes.map((n) => n.id).includes('agent_session:s1'));
  assert.equal(viewModel(SNAP, { query: 'nothing like this' }).nodes.length, 0);
  assert.match(renderBar(SNAP, { kind: 'task' }, 'project:p'), /<option value="task" selected>/);
  assert.match(renderBar(SNAP, {}, 'project:p'), /\(1 folded away\)/);
});

test('every state has its own plain message', () => {
  const base = { snapshot: SNAP, positions: {}, moved: new Set(), filters: {} };
  assert.match(renderBody({ phase: 'idle' }), /data-tm-state="idle"/);
  assert.match(renderBody({ phase: 'off' }), /not turned on/);
  assert.doesNotMatch(renderBody({ phase: 'off' }), /error|failed/i);
  assert.match(renderBody({ phase: 'unavailable', message: 'down' }), /not available right now \(down\)/);
  assert.match(renderBody({ phase: 'error', message: '<x>' }), /could not be loaded: &lt;x&gt;/);
  assert.match(renderBody({ ...base, phase: 'ok', snapshot: { nodes: [], edges: [] }, vm: viewModel({ nodes: [] }) }), /data-tm-state="empty"[^>]*>Nothing is recorded for this project yet/);
  assert.match(renderBody({ ...base, phase: 'ok', vm: viewModel(SNAP, { query: 'zzz' }) }), /data-tm-state="nomatch"/);
  const vm = viewModel(SNAP);
  assert.match(renderBody({ ...base, phase: 'ok', snapshot: { ...SNAP, has_more: true }, vm, positions: placeNodes(vm.nodes) }), /data-tm-truncated/);
});

test('a big map draws at most MAX_DRAWN nodes and says so, while the written map lists all', () => {
  const nodes = [node('project:p', 'project'), ...Array.from({ length: 700 }, (_, i) => node(`task:t${String(i).padStart(4, '0')}`, 'task'))];
  const snap = { nodes, edges: nodes.slice(1).map((n, i) => edge(`c${i}`, 'contains', 'project:p', n.id)) };
  const vm = viewModel(snap);
  const html = renderCanvas(vm, placeNodes(vm.nodes));
  assert.equal((html.match(/role="button"/g) || []).length, MAX_DRAWN);
  assert.match(html, /data-tm-capped>Showing the first 300 of 701/);
  assert.equal((renderText(vm).match(/data-open-node/g) || []).length, 701);
});

test('placing new nodes never moves anything already placed (saved, personal or drawn before)', () => {
  const vm = viewModel(SNAP);
  const saved = { shared: { 'task:t1': { x: 440, y: 0 } }, mine: { 'issue:i1': { x: 220, y: 144 } } };
  const first = placeNodes(vm.nodes, saved);
  assert.deepEqual(first['task:t1'], { x: 440, y: 0 });
  assert.deepEqual(first['issue:i1'], { x: 220, y: 144 });
  const second = placeNodes(viewModel(SNAP, { showExecution: true }).nodes, { ...saved, prior: first });
  for (const [id, p] of Object.entries(first)) assert.deepEqual(second[id], p, `${id} stayed`);
  assert.ok(second['agent_session:s1']);
  assert.deepEqual(placeNodes(vm.nodes), placeNodes(vm.nodes.slice().reverse()), 'deterministic');
});

test('moving is by whole cells and refused onto another node or off the canvas', () => {
  const pos = placeNodes(viewModel(SNAP).nodes);
  const before = JSON.stringify(pos);
  const moved = moveNode(pos, 'task:t2', 0, 3);
  assert.deepEqual(moved['task:t2'], { x: pos['task:t2'].x, y: pos['task:t2'].y + 3 * CELL.h });
  assert.equal(JSON.stringify(pos), before, 'input untouched');
  assert.equal(moveNode(pos, 'task:t2', 0, -1), null, 'task:t1 sits there');
  assert.equal(moveNode(pos, 'project:p', -1, 0), null);
  assert.equal(moveNode(pos, 'nope', 1, 0), null);
});

test('the saved document keeps every saved entry as is, changing only moved nodes and adding unsaved ones', () => {
  const shared = { 'issue:i1': { x: 220, y: 0 } };
  const pos = placeNodes(viewModel(SNAP).nodes, { mine: { 'task:t1': { x: 440, y: 0 } }, shared });
  const doc = layoutToSave({ nodes: { 'task:t1': { x: 440, y: 0 }, 'gone:x': { x: 1, y: 1 } }, note: 'keep' }, { ...pos, 'task:t2': { x: 440, y: 288 } }, { shared, moved: new Set(['task:t2']) });
  assert.deepEqual(doc.nodes['task:t1'], { x: 440, y: 0 });
  assert.deepEqual(doc.nodes['gone:x'], { x: 1, y: 1 }, 'entries for nodes not on screen survive');
  assert.deepEqual(doc.nodes['task:t2'], { x: 440, y: 288 });
  assert.equal(doc.nodes['issue:i1'], undefined, 'a shared position is not copied into the personal layout');
  assert.ok(doc.nodes['project:p'], 'a node with no saved position is stored where it was drawn');
  assert.equal(doc.note, 'keep');
});

test('against a real server: loads the snapshot and layouts, then saves with the ETag', async () => {
  const s = await serve({ layouts: { default: { nodes: { 'task:t1': { x: 440, y: 0 } } } } });
  try {
    const loaded = await loadTeamMap(s.client, 'project:p');
    assert.equal(loaded.status, 'ok');
    assert.equal(loaded.snapshot.nodes.length, SNAP.nodes.length);
    assert.deepEqual(loaded.shared['task:t1'], { x: 440, y: 0 });
    assert.equal(loaded.layoutReadable, true);
    let positions = placeNodes(viewModel(loaded.snapshot).nodes, { shared: loaded.shared, mine: loaded.mine.doc.nodes });
    assert.deepEqual(positions['task:t1'], { x: 440, y: 0 }, 'the shared position is used');
    positions = moveNode(positions, 'task:t2', 0, 2);
    const saved = await saveTeamMapLayout(s.client, 'project:p', { mine: loaded.mine, positions, shared: loaded.shared, moved: new Set(['task:t2']) });
    assert.equal(saved.ok, true);
    assert.equal(s.seen.puts[0].shared, false);
    assert.equal(s.seen.puts[0].if_match, '"v0"');
    assert.deepEqual(s.docs.mine.nodes['task:t2'], positions['task:t2']);
    assert.equal(s.docs.mine.nodes['task:t1'], undefined, 'shared position not duplicated');
    assert.equal(saved.mine.etag, '"v1"');
    const again = await saveTeamMapLayout(s.client, 'project:p', { mine: saved.mine, positions, shared: loaded.shared, moved: new Set() });
    assert.equal(again.ok, true);
    assert.deepEqual(s.docs.mine.nodes['task:t2'], positions['task:t2'], 'saving again moves nothing');
  } finally { s.close(); }
});

test('against a real server: a stale ETag is refused and asks for a reload', async () => {
  const s = await serve();
  try {
    const loaded = await loadTeamMap(s.client, 'project:p');
    s.bump();
    const res = await saveTeamMapLayout(s.client, 'project:p', { mine: loaded.mine, positions: placeNodes(viewModel(SNAP).nodes), shared: {}, moved: new Set() });
    assert.equal(res.ok, false);
    assert.match(res.message, /Reload the map/);
  } finally { s.close(); }
});

test('against a real server: the flag off (404) is the plain not-enabled state, not an error', async () => {
  const s = await serve({ enabled: false });
  try {
    const loaded = await loadTeamMap(s.client, 'project:p');
    assert.deepEqual(loaded, { status: 'off' });
    const html = renderBody({ phase: loaded.status });
    assert.match(html, /data-tm-state="off"/);
    assert.doesNotMatch(html, /could not be loaded/);
  } finally { s.close(); }
  const on = await serve();
  try { assert.equal((await loadTeamMap(on.client, 'project:other')).status, 'off', 'a hidden or missing project looks the same'); } finally { on.close(); }
});

test('an unreachable server is an error state', async () => {
  const res = await loadTeamMap(new HerdrClient('http://127.0.0.1:1'), 'project:p');
  assert.equal(res.status, 'error');
});
