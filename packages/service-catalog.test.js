import test from 'node:test';
import assert from 'node:assert/strict';
import { SERVICES, describeServices } from './service-catalog.js';

const keys = (list) => list.map((s) => s.capability);

test('every service names a capability, a label and, unless always on, the config key that enables it', () => {
  assert.equal(new Set(keys(SERVICES)).size, SERVICES.length, 'capabilities are unique');
  for (const s of SERVICES) {
    assert.match(s.capability, /^[a-z_0-9]+$/);
    assert.ok(s.label && s.description);
    if (!s.alwaysOn) assert.match(s.configKey, /^control_plane\.[a-z_]+(\.[a-z_]+)*$/, `${s.capability} has a config key`);
  }
});

test('the catalog covers the service flags the server advertises', () => {
  for (const cap of ['share_v1', 'members_v1', 'collab_artifacts_v1', 'review_gate_v1', 'artifact_delete_v1', 'ci_actions_v1',
    'deployment_actions_v1', 'assistant_v1', 'memory_v1', 'architecture_v1', 'deployments_v1', 'collab_v1']) {
    assert.ok(keys(SERVICES).includes(cap), `${cap} is listed`);
  }
});

test('describeServices marks advertised services on and the rest off, with how to enable only for off ones', () => {
  const rows = describeServices(['share_v1', 'ci_v1']);
  const share = rows.find((r) => r.capability === 'share_v1');
  assert.equal(share.enabled, true);
  assert.equal(share.howToEnable, '');
  const members = rows.find((r) => r.capability === 'members_v1');
  assert.equal(members.enabled, false);
  assert.equal(members.howToEnable, 'control_plane.collab.members.enabled');
  assert.equal(rows.length, SERVICES.length);
});

test('unknown input is treated as nothing advertised', () => {
  assert.equal(describeServices(undefined).find((r) => r.capability === 'share_v1').enabled, false);
});

test('rows carry only names, labels and booleans, never configuration values', () => {
  for (const row of describeServices(['share_v1'])) {
    assert.deepEqual(Object.keys(row).sort(),
      ['alwaysOn', 'capability', 'configKey', 'description', 'enabled', 'group', 'howToEnable', 'label']);
  }
});
