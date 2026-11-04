import test from 'node:test';
import assert from 'node:assert/strict';
import { renderSchedulingView } from './scheduler-view.js';

test('history escapes request identities and distinguishes claims from running processes', () => {
  const html = renderSchedulingView({claimed_executions: 1, history: [
    {request_id: '<img src=x>', state: 'committed', host_uid: 'host-b', execution_claimed: true},
  ]});
  assert.match(html, /&lt;img src=x&gt;/);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /A claim does not confirm a running process/);
  assert.match(html, /execution claimed/);
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

