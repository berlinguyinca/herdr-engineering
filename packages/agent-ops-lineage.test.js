import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { HerdrClient } from './herdr-web-client/client.js';
import { lineageEnabled, lineageModel, renderLineageCard, mapHref, mountAgentOpsLineage } from './agent-ops-lineage.js';

const agent = (id, label) => ({ node_id: id, agent_id: id, label, model: 'opus', state: 'working', room_ids: [], context: null, host_id: 'h1' });
const SNAP = {
  agents: [agent('a1', 'Planner <b>'), agent('a2', 'Coder'), agent('a3', 'Reviewer')],
  edges: [{ kind: 'review', from: 'a3', to: 'a1', state: 'requested', task_id: 't1' }],
  tasks: [{ task_id: 't1', accountable_owner: 'a1', contributors: ['a2'], reviewer: 'a3', updated_at: '2026-11-12T10:00:00Z' },
    { task_id: 't2', accountable_owner: 'zz-unknown-agent-id', contributors: [], reviewer: null, updated_at: '2026-11-12T09:00:00Z' }],
};

test('gate needs BOTH capabilities', () => {
  assert.equal(lineageEnabled(['agent_ops_v1', 'work_center_v1']), true);
  assert.equal(lineageEnabled(['agent_ops_v1']), false);
  assert.equal(lineageEnabled(['work_center_v1']), false);
  assert.equal(lineageEnabled(undefined), false);
});

test('model: owner, contributors, reviewer, review state only when present; unknown ids shortened, newest first', () => {
  const rows = lineageModel(SNAP);
  assert.deepEqual(rows.map((r) => r.id), ['t1', 't2']);
  assert.equal(rows[0].owner, 'Planner <b>'); assert.deepEqual(rows[0].contributors, ['Coder']); assert.equal(rows[0].reviewer, 'Reviewer'); assert.equal(rows[0].reviewState, 'requested');
  assert.equal(rows[1].reviewState, null); assert.equal(rows[1].reviewer, ''); assert.notEqual(rows[1].owner, 'zz-unknown-agent-id');
  assert.equal(lineageModel(SNAP, 1).length, 1); assert.deepEqual(lineageModel(null), []);
});

test('card: escapes, links to the map, states absence honestly', () => {
  const html = renderLineageCard(lineageModel(SNAP), { total: 3 });
  assert.ok(!html.includes('<b>')); assert.ok(html.includes('&lt;b&gt;'));
  assert.ok(html.includes(`href="${mapHref('t1')}"`)); assert.ok(html.includes('review requested')); assert.ok(html.includes('none reported')); assert.ok(html.includes('1 more tasks'));
  assert.ok(renderLineageCard([]).includes('No tasks reported'));
});

function serve(code) {
  const server = http.createServer((req, res) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(code === 200 ? SNAP : { error: 'x' })); });
  return new Promise((r) => server.listen(0, '127.0.0.1', () => r({ client: new HerdrClient(`http://127.0.0.1:${server.address().port}`), close: () => server.close() })));
}

test('mount: real server + real client paints the card; 404 leaves the slot empty', async () => {
  const ok = await serve(200), off = await serve(404);
  try {
    const a = { innerHTML: '' }, b = { innerHTML: '' };
    const ma = mountAgentOpsLineage(a, ok.client); await ma.refresh();
    assert.ok(a.innerHTML.includes('data-overview="agent-lineage"')); assert.equal(a.innerHTML.split('class="ao-lineage-row"').length - 1, 2);
    const mb = mountAgentOpsLineage(b, off.client); await mb.refresh();
    assert.equal(b.innerHTML, '');
    ma.dispose(); assert.equal(a.innerHTML, '');
  } finally { ok.close(); off.close(); }
});
