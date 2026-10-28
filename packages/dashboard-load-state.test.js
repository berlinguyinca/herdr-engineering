import test from 'node:test';
import assert from 'node:assert/strict';
import {classifyDashboardError, dashboardErrorMessage, requestErrorDetail} from './dashboard-load-state.js';

test('classifies an aborted deadline as a timeout with elapsed time', () => {
  const error = Object.assign(new Error('The request timed out'), {name: 'AbortError', dashboardTimeout: true});
  assert.deepEqual(classifyDashboardError(error, 15123), {
    kind: 'timeout', status: 0, elapsedMs: 15123, message: 'The request timed out',
  });
});

test('classifies structured HTTP failures by actionable cause', () => {
  assert.equal(classifyDashboardError({status: 429, message: 'too many requests'}).kind, 'rate_limited');
  assert.equal(classifyDashboardError({status: 503, message: 'database unavailable'}).kind, 'backend');
  assert.equal(classifyDashboardError({status: 401}).kind, 'authentication');
});

test('renders a useful cause instead of a generic refresh failure', () => {
  assert.match(dashboardErrorMessage('Services', {kind: 'timeout', elapsedMs: 15000}), /Services failed after 15s/);
  assert.match(dashboardErrorMessage('Services', {kind: 'timeout', elapsedMs: 15000}), /exceeded the dashboard deadline/);
});

test('request diagnostics retain the backend cause, elapsed time and correlation ID', () => {
  assert.equal(requestErrorDetail({ message: 'database unavailable', elapsedMs: 12050, requestId: 'req-123' }), 'database unavailable after 12.1s · Request req-123');
  assert.equal(requestErrorDetail(new Error('Failed to fetch')), 'Failed to fetch');
});
