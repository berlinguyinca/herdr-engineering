import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { HerdrClient } from './herdr-web-client/client.js';
import { serviceOff, retireWhenOff } from './service-gates.js';

// A real HTTP server answering the health probe, with the real client against it.
async function withHealth(handler, run) {
  const server = http.createServer(handler);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try { await run(new HerdrClient(`http://127.0.0.1:${server.address().port}`)); } finally { server.close(); }
}
const advertise = (capabilities) => (req, res) => {
  res.writeHead(200, { 'content-type': 'application/json' });
  res.end(JSON.stringify({ status: 'ok', capabilities }));
};

test('a service is off only when the probe answered without its capability', async () => {
  await withHealth(advertise(['share_v1']), async (client) => {
    assert.equal(await serviceOff(client, 'share_v1'), false);
    assert.equal(await serviceOff(client, 'members_v1'), true);
  });
});

test('a failed probe never reports a service as off, so a transient error cannot hide a working panel', async () => {
  await withHealth((req, res) => { res.writeHead(500); res.end('{}'); }, async (client) => {
    assert.equal(await serviceOff(client, 'share_v1'), false);
  });
});

test('the probe is shared between callers of one client', async () => {
  let hits = 0;
  await withHealth((req, res) => { hits += 1; advertise(['share_v1'])(req, res); }, async (client) => {
    await Promise.all([serviceOff(client, 'share_v1'), serviceOff(client, 'members_v1'), serviceOff(client, 'ci_actions_v1')]);
    assert.equal(hits, 1);
  });
});

test('retireWhenOff removes the elements of an off service and keeps those of an advertised one', async () => {
  await withHealth(advertise(['share_v1']), async (client) => {
    const removed = [];
    const element = (name) => ({ remove: () => removed.push(name) });
    await retireWhenOff(client, 'share_v1', element('share'));
    await retireWhenOff(client, 'members_v1', element('members'), element('members-2'));
    assert.deepEqual(removed, ['members', 'members-2']);
  });
});
