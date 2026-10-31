import test from 'node:test';
import assert from 'node:assert/strict';
import { renderSchedulingView } from './scheduler-view.js';

test('eligibility metrics exclude stale automatic hosts', () => {
  const html = renderSchedulingView({ hosts: [{ host_uid: 'stale', trusted: true,
    owned: true, reachable: true, policy: { mode: 'automatic' },
    telemetry: { freshness_seconds: 90 } }] });
  assert.match(html, /label="Eligible hosts" value="0"/);
});

test('execution claim metric does not present claims as running processes', () => {
  const html = renderSchedulingView({ hosts: [], claimed_executions: 2, running_agents: 99 });
  assert.match(html, /label="Claimed executions" value="2"/);
  assert.doesNotMatch(html, /label="Running agents"/);
});

test('scheduling view labels mac manual-only and stale telemetry explicitly', () => {
  const html = renderSchedulingView({ hosts: [
    { host_uid: 'mac-uid', host_name: 'mac.lan', platform: 'darwin', trusted: true, owned: true, reachable: true,
      policy: { mode: 'manual_only', revision: 3 }, telemetry: { freshness_seconds: 61 }, capacity: {} },
    { host_uid: 'beast-uid', host_name: 'beast', platform: 'linux', trusted: true, owned: true, reachable: true,
      policy: { mode: 'automatic', revision: 2 }, telemetry: { freshness_seconds: 2 }, capacity: { total_cpu: 16 } },
  ] });
  assert.match(html, /Manual only/);
  assert.match(html, /stale telemetry/);
  assert.match(html, /Eligible hosts/);
  assert.match(html, /Eligible hosts/);
  assert.match(html, /Preview placement/);
});

