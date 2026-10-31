import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { HerdrClient } from './herdr-web-client/client.js';
import { loadAssistantGraph, renderAssistantGraph } from './assistant-graph-view.js';

const GRAPH = {
  nodes: [
    { id: 'you', kind: 'you', name: 'You' },
    { id: 'asst_1', kind: 'assistant', name: 'Fleet <PA>', status: 'ready', enabled: true, can_delegate: true, delegate_scopes: ['pi-secretary'] },
    { id: 'asst_2', kind: 'assistant', name: 'Mail assistant', status: 'ready', enabled: true, can_delegate: false, delegate_scopes: [] },
    { id: 'specialist:pi-secretary', kind: 'specialist', name: 'pi-secretary', bound: false, role: 'procedure:secretary-system@1.0.0' },
    { id: 'specialist:pi', kind: 'specialist', name: 'pi', bound: true, role: null },
    { id: 'dlg_1', kind: 'delegation', status: 'running', name: 'Find a time <script>x</script>' },
  ],
  edges: [
    { from: 'you', to: 'asst_1', kind: 'owns' }, { from: 'you', to: 'asst_2', kind: 'owns' },
    { from: 'asst_1', to: 'dlg_1', kind: 'delegated' }, { from: 'dlg_1', to: 'specialist:pi-secretary', kind: 'runs_on' },
  ],
};

function serve(mode = 'ok') {
  const server = http.createServer((req, res) => {
    const send = (code, body) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(body)); };
    if (req.url !== '/api/v1/assistant-graph' || mode === 'off') return send(404, { error: 'not found' });
    if (mode === 'broken') return send(500, { error: 'internal error' });
    return send(200, GRAPH);
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve({
    client: new HerdrClient(`http://127.0.0.1:${server.address().port}`), close: () => server.close(),
  })));
}

test('the real client loads the graph from the real route contract', async () => {
  const s = await serve();
  try {
    const out = await loadAssistantGraph(s.client);
    assert.equal(out.status, 'ok');
    assert.equal(out.nodes.length, 6);
    assert.equal(out.edges.length, 4);
  } finally { s.close(); }
});

test('a missing route is a plain off state and a server error is an error state', async () => {
  const off = await serve('off');
  const broken = await serve('broken');
  try {
    assert.equal((await loadAssistantGraph(off.client)).status, 'off');
    assert.equal((await loadAssistantGraph(broken.client)).status, 'error');
  } finally { off.close(); broken.close(); }
});

test('assistants show who can delegate, delegations nest under their assistant, specialists show readiness', () => {
  const html = renderAssistantGraph({ phase: 'ready', ...GRAPH });
  assert.match(html, /data-graph-id="asst_1"[\s\S]*can delegate: pi-secretary/);
  assert.match(html, /data-graph-id="asst_2"[\s\S]*cannot delegate yet/);
  const first = html.indexOf('data-graph-id="asst_1"');
  const second = html.indexOf('data-graph-id="asst_2"');
  const dlg = html.indexOf('data-graph-id="dlg_1"');
  assert.ok(first < dlg && dlg < second, 'the delegation sits under the assistant that made it');
  assert.match(html, /on pi-secretary/);
  assert.match(html, /data-graph-id="specialist:pi-secretary"[\s\S]*not set up/);
  assert.match(html, /data-graph-id="specialist:pi"[\s\S]*ready/);
});

test('names from data are escaped', () => {
  const html = renderAssistantGraph({ phase: 'ready', ...GRAPH });
  assert.ok(!html.includes('<script>') && !html.includes('Fleet <PA>'));
  assert.match(html, /Fleet &lt;PA&gt;/);
});

test('loading, off, error and empty states render without a graph', () => {
  assert.match(renderAssistantGraph({ phase: 'off' }), /data-graph-state="off"/);
  assert.match(renderAssistantGraph({ phase: 'error', message: 'boom <b>' }), /boom &lt;b&gt;/);
  assert.match(renderAssistantGraph({ phase: 'ready', nodes: [], edges: [] }), /No assistants yet/);
  assert.ok(renderAssistantGraph({ phase: 'loading' }).length > 0);
});
