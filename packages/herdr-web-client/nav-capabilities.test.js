import { test } from 'node:test';
import assert from 'node:assert/strict';
import { visibleNav } from '../nav-capabilities.js';

const NAV = [
  { path: '/overview', label: 'Overview' },
  { path: '/architecture', label: 'Architecture', capability: 'architecture_v1' },
];

test('a capability-gated entry is hidden until the server advertises it', () => {
  assert.deepEqual(visibleNav(NAV, undefined).map((e) => e.path), ['/overview']);
  assert.deepEqual(visibleNav(NAV, ['architecture_explain_v1']).map((e) => e.path), ['/overview']);
  assert.deepEqual(visibleNav(NAV, ['architecture_v1']),
    [{ path: '/overview', label: 'Overview' }, { path: '/architecture', label: 'Architecture' }]);
});
