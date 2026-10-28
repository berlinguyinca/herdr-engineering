import {test} from 'node:test';
import assert from 'node:assert/strict';
import {reportView, reportStartError, reportFailure} from './summary-report-status.js';

test('idle, running, done and failed map to one button each', () => {
  assert.deepEqual(reportView(null, null), {label: 'Generate full report', locked: false, status: '', report: ''});
  assert.equal(reportView({state: 'running'}, null).locked, true);
  assert.equal(reportView({state: 'idle'}, {state: 'starting'}).locked, true);
  const done = reportView({state: 'done', report: '# Session summary', harness: 'claude', generated_at: 'T'}, null);
  assert.equal(done.label, 'Regenerate full report');
  assert.equal(done.report, '# Session summary');
  assert.match(done.status, /claude · T/);
  const failed = reportView({state: 'failed', error: 'timeout', detail: 'did not finish within 300s'}, null);
  assert.equal(failed.error, true);
  assert.match(failed.status, /did not finish in time\. did not finish within 300s\./);
});

test('an old report stays visible while a new one runs or a start fails', () => {
  assert.equal(reportView({state: 'running', report: 'old'}, null).report, 'old');
  const err = reportStartError(Object.assign(new Error('on beast'), {body: {reason: 'report_host_remote', command: 'herdr-eng summary report --agent claude --session x'}}));
  const view = reportView({state: 'done', report: 'old'}, err);
  assert.equal(view.report, 'old');
  assert.equal(view.error, true);
  assert.match(view.command, /^herdr-eng summary report/);
});

test('unsaved reports say so and unknown codes stay generic', () => {
  assert.match(reportView({state: 'done', report: 'r', persisted: false}, null).status, /not saved/);
  assert.equal(reportFailure('weird'), 'The report could not be generated.');
  assert.equal(reportStartError(new Error('x')).command, '');
});
