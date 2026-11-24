import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { HerdrClient } from './herdr-web-client/client.js';
import { loadHosts, renderMatrixRegion, retryError } from './hosts-view.js';
import { loadHostDetail } from './host-detail-view.js';
import { configNotice } from './fleet-config-badge.js';

// A real HTTP server and the real HerdrClient: each route answers with the status given.
async function serve(routes) {
  const server = http.createServer((req, res) => {
    const path = req.url.split('?')[0];
    const [status, body] = routes[path] ?? [404, { error: 'not found' }];
    res.writeHead(status, { 'content-type': 'application/json' });
    res.end(JSON.stringify(body));
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const client = new HerdrClient(`http://127.0.0.1:${server.address().port}`);
  return { client, close: () => new Promise((r) => server.close(r)) };
}
const ok = (body) => [200, body];
const HOSTS = ok({ hosts: [{ host_id: 'beast' }] });
const MATRIX = ok({ hosts: [{ host: 'beast', collected_at: null }], components: [], summary: {} });

test('loadHosts reports a failed inventory fetch instead of turning it into empty data', async () => {
  const s = await serve({ '/api/v1/hosts': HOSTS, '/api/v1/fleet/inventory': [500, { error: 'boom' }], '/api/v1/fleet/config/status': ok({ hosts: [] }) });
  try {
    const data = await loadHosts(s.client);
    assert.ok(data.matrixError, 'inventory failure is carried');
    assert.equal(data.matrix, null);
    assert.equal(data.hosts.length, 1);
  } finally { await s.close(); }
});

test('loadHosts reports a failed config-status fetch; a 404 (no fleet config) is not a failure', async () => {
  const bad = await serve({ '/api/v1/hosts': HOSTS, '/api/v1/fleet/inventory': MATRIX, '/api/v1/fleet/config/status': [500, { error: 'x' }] });
  const none = await serve({ '/api/v1/hosts': HOSTS, '/api/v1/fleet/inventory': MATRIX });
  try {
    assert.ok((await loadHosts(bad.client)).configError);
    const clean = await loadHosts(none.client);
    assert.equal(clean.configError, null);
    assert.equal(clean.matrixError, null);
    assert.equal(clean.matrix.hosts.length, 1);
  } finally { await bad.close(); await none.close(); }
});

test('an inventory error renders an error with Retry, never the "no host has reported" empty state', () => {
  const html = renderMatrixRegion({ matrix: null, matrixError: 'HTTP 500' }, {});
  assert.match(html, /<herdr-error/);
  assert.match(html, /data-hosts-retry/);
  assert.doesNotMatch(html, /No host has reported/);
});

test('a genuinely empty inventory still says no host has reported', () => {
  assert.match(renderMatrixRegion({ matrix: { hosts: [], components: [] }, matrixError: null }, {}), /No host has reported/);
});

test('retryError escapes the message', () => {
  assert.match(retryError('<img onerror=x>', 'data-x-retry'), /&lt;img/);
});

test('config notice surfaces a failed config-status read', () => {
  assert.match(configNotice(null, 'HTTP 500'), /could not be read/);
  assert.equal(configNotice(null, null), '');
});

test('loadHostDetail carries a failed telemetry read and a 404 host stays null', async () => {
  const s = await serve({ '/api/v1/hosts/beast': ok({ host_id: 'beast' }), '/api/v1/hosts/beast/telemetry': [500, {}] });
  try {
    const d = await loadHostDetail(s.client, 'beast', '1h');
    assert.ok(d.telemetryError);
    assert.deepEqual(d.telemetry, { samples: [] });
    assert.equal((await loadHostDetail(s.client, 'gone', '1h')).host, null);
  } finally { await s.close(); }
});
