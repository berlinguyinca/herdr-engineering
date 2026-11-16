import test from 'node:test';
import assert from 'node:assert/strict';
import { renderPipeline } from './integrations-view.js';

const run = { run_number: 7, repository: 'org/repo', state: 'failure', can_retry: true, can_cancel: true, tasks: [] };

test('Retry and Cancel are offered when the run allows them and ci_actions_v1 is advertised', () => {
  const html = renderPipeline(run);
  assert.match(html, /data-ci-action="retry"/);
  assert.match(html, /data-ci-action="cancel"/);
});

test('Retry and Cancel disappear while the server does not advertise ci_actions_v1', () => {
  const html = renderPipeline(run, { ciActions: false });
  assert.doesNotMatch(html, /data-ci-action/);
});
