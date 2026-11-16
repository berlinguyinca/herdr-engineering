import test from 'node:test';
import assert from 'node:assert/strict';
import { coordinatorLine, fabricAgentLine, fabricBadge, fabricState } from './collab-fabric.js';

const EVIL = '<img src=x onerror=alert(1)>"\'&';
const NOW = 1_800_000_000;

test('the badge shows only for via=fabric and escapes the machine', () => {
  assert.equal(fabricBadge({ event_type: 'message' }), '');
  assert.equal(fabricBadge({ via: 'other', machine: 'x' }), '');
  assert.equal(fabricBadge(null), '');
  assert.match(fabricBadge({ via: 'fabric', machine: 'bender' }), /via fabric · bender/);
  const html = fabricBadge({ via: 'fabric', machine: EVIL });
  assert.ok(!html.includes('<img') && html.includes('&lt;img'));
  assert.match(fabricBadge({ via: 'fabric', machine: null }), /via fabric<\/span>/);
});

test('fabric state is off, active or expired (no expiry recorded fails closed)', () => {
  assert.equal(fabricState({ fabric: false }, NOW), 'off');
  assert.equal(fabricState({ fabric: true, fabric_expires_at: NOW + 5 }, NOW), 'active');
  assert.equal(fabricState({ fabric: true, fabric_expires_at: NOW - 5 }, NOW), 'expired');
  assert.equal(fabricState({ fabric: true, fabric_expires_at: null }, NOW), 'expired');
});

test('the agent line names machine, bit and expiry, and escapes', () => {
  assert.equal(fabricAgentLine({ machine: null, fabric: false }, NOW), '');
  assert.match(fabricAgentLine({ machine: 'beast', fabric: false }, NOW), /machine beast · fabric off/);
  assert.match(fabricAgentLine({ machine: 'beast', fabric: true, fabric_expires_at: NOW + 99 }, NOW), /fabric until/);
  assert.match(fabricAgentLine({ machine: 'beast', fabric: true, fabric_expires_at: NOW - 1 }, NOW), /data-fabric-expired>fabric expired/);
  assert.ok(!fabricAgentLine({ machine: EVIL, fabric: true }, NOW).includes('<img'));
});

test('an auto-joined agent is marked so the operator can see and revoke it', () => {
  const line = fabricAgentLine({ machine: 'bender', fabric: true, fabric_expires_at: NOW + 99, origin: 'fabric_join' }, NOW);
  assert.match(line, /data-fabric-autojoined>auto-joined/);
  assert.ok(!fabricAgentLine({ machine: 'bender', fabric: true, fabric_expires_at: NOW + 99 }, NOW).includes('auto-joined'));
  assert.ok(!fabricAgentLine({ machine: 'bender', fabric: true, origin: '<img>' }, NOW).includes('auto-joined'));
});

test('the coordinator line shows team and roles and escapes', () => {
  assert.equal(coordinatorLine(undefined), '');
  assert.match(coordinatorLine({ team_id: 'team-abc', roles: ['implementer', 'reviewer'] }), /coordinator: team-abc · roles implementer, reviewer/);
  assert.match(coordinatorLine({ team_id: 't', roles: [] }), /roles none/);
  assert.ok(!coordinatorLine({ team_id: EVIL, roles: [EVIL] }).includes('<img'));
});
