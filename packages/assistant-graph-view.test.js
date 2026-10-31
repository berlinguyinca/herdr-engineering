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


const PLACED = {
  nodes: [
    { id: 'you', kind: 'you', name: 'You' },
    { id: 'asst_p', kind: 'assistant', name: 'Fleet PA', type: 'rakazo', status: 'ready', can_delegate: false, delegate_scopes: [] },
    { id: 'asst_s', kind: 'assistant', name: 'Secretary', type: 'pi-secretary', parent_id: 'asst_p', status: 'needs_setup',
      can_delegate: false, delegate_scopes: [], placement: { state: 'planned', host: 'host:bender' } },
    { id: 'asst_w', kind: 'assistant', name: 'Web <scout>', type: 'scout', parent_id: 'asst_p', status: 'needs_setup',
      can_delegate: false, delegate_scopes: [], placement: { state: 'unplaceable', needs: ['display'], reasons: ['host:bender: missing display', '<b>x</b>'] } },
    { id: 'asst_c', kind: 'assistant', name: 'Cluster job', type: 'pi-secretary', status: 'needs_setup',
      can_delegate: false, delegate_scopes: [], placement: { state: 'planned', host: 'slurm:hive:low' } },
    { id: 'host:bender', kind: 'host', name: 'bender', backend: 'host', status: 'ready', capabilities: ['display', 'internet'], free: { cpus: 8, memory_mb: 16384, gpus: 0 } },
    { id: 'slurm:hive:low', kind: 'host', name: 'Hive / low', backend: 'slurm', status: 'ready', capabilities: ['os:linux'], free: { cpus: 640, memory_mb: 5120000, gpus: 0 } },
  ],
  edges: [
    { from: 'you', to: 'asst_p', kind: 'owns' }, { from: 'asst_p', to: 'asst_s', kind: 'linked' }, { from: 'asst_p', to: 'asst_w', kind: 'linked' },
    { from: 'asst_s', to: 'host:bender', kind: 'placed_on', state: 'planned' },
  ],
};

test('linked children nest under their parent and unlinked assistants stay at the top', () => {
  const html = renderAssistantGraph({ phase: 'ready', ...PLACED });
  const parent = html.indexOf('data-graph-id="asst_p"');
  const child = html.indexOf('data-graph-id="asst_s"');
  const other = html.indexOf('data-graph-id="asst_c"');
  assert.ok(parent < child && child < other, 'the child sits under its parent; the unlinked assistant follows');
  assert.equal((html.match(/data-graph-id="asst_s"/g) || []).length, 1, 'a linked child is drawn once');
  assert.match(html, /data-graph-id="asst_s"[\s\S]*pi-secretary/);
});

test('where an agent would run is labelled planned, with the backend, and never claims it is running', () => {
  const html = renderAssistantGraph({ phase: 'ready', ...PLACED });
  assert.match(html, /planned: bender/);
  assert.match(html, /planned: Hive \/ low[\s\S]*slurm/);
  assert.ok(!/running on|runs on bender/.test(html));
});

test('an unplaceable specialist says why, escaped', () => {
  const html = renderAssistantGraph({ phase: 'ready', ...PLACED });
  assert.match(html, /can&#39;t be placed|can't be placed/);
  assert.match(html, /host:bender: missing display/);
  assert.ok(!html.includes('<b>x</b>') && !html.includes('Web <scout>'));
  assert.match(html, /Web &lt;scout&gt;/);
});

test('hosts are listed with status, backend, capabilities and free resources', () => {
  const html = renderAssistantGraph({ phase: 'ready', ...PLACED });
  assert.match(html, /Where agents run/);
  assert.match(html, /data-graph-id="host:bender"[\s\S]*display[\s\S]*internet/);
  assert.match(html, /free: 8 cpu, 16 GiB/);
  assert.match(html, /data-graph-id="slurm:hive:low"[\s\S]*slurm/);
  assert.ok(!renderAssistantGraph({ phase: 'ready', nodes: [{ id: 'you', kind: 'you' }], edges: [] }).includes('Where agents run'));
});
