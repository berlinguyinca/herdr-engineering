import test from 'node:test';
import assert from 'node:assert/strict';
import { filterLeases, renderFabricServices } from '../fabric-services-view.js';

const L = [
  { id: 'a', state: 'active', label: 'web', url: 'https://dev.lan:3188', target: { machine_id: 'bender', host: '127.0.0.1', port: 3188 }, owner: { herdr_session_id: 's1' } },
  { id: 'b', state: 'active', label: 'api', url: 'javascript:alert(1)', health_last_error: 'refused', target: { machine_id: 'beast', host: '100.64.0.2', port: 9 }, owner: {} },
];

test('search matches name, host, address and owner', () => {
  assert.deepEqual(filterLeases(L, 'bender').map((r) => r.id), ['a']);
  assert.deepEqual(filterLeases(L, 'S1').map((r) => r.id), ['a']);
  assert.deepEqual(filterLeases(L, 'unhealthy').map((r) => r.id), ['b']);
  assert.equal(filterLeases(L, '').length, 2);
});

test('unsafe or missing URLs never become links and errors are shown', () => {
  const html = renderFabricServices({ available: true, leases: L });
  assert.ok(html.includes('href="https://dev.lan:3188/"'));
  assert.ok(!html.includes('javascript:'));
  assert.ok(html.includes('title="refused"') && html.includes('fabric-state--unhealthy'));
});

test('empty and gateway-unavailable states are explained', () => {
  assert.ok(renderFabricServices({ available: false, leases: [] }).includes('not reachable'));
  assert.ok(renderFabricServices({ available: true, leases: [] }).includes('No services'));
  assert.ok(renderFabricServices({ available: true, leases: L }, 'zzz').includes('No fabric service matches'));
});
