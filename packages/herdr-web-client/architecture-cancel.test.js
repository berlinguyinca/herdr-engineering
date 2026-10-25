import {test} from 'node:test';
import assert from 'node:assert/strict';
import {cancellableJobs, jobsPanel, cancelJob} from '../architecture-view.js';
import {HerdrClient} from './client.js';

const jobs = [
  {analysis_id: 'an-req', state: 'requested', repository_id: 'github.com/o/r', rev: 'main', host_id: 'h1'},
  {analysis_id: 'an-run', state: 'progress', host_id: 'h2'},
  {analysis_id: 'an-done', state: 'completed'}, {analysis_id: 'an-fail', state: 'failed'},
  {analysis_id: 'an-gone', state: 'cancelled'}, {analysis_id: 'an-exp', state: 'expired'},
];

test('only unfinished jobs are cancellable', () => {
  assert.deepEqual(cancellableJobs(jobs).map(j => j.analysis_id), ['an-req', 'an-run']);
  assert.deepEqual(cancellableJobs([null, {state: 'requested'}]), []);
  assert.equal(jobsPanel([{analysis_id: 'x', state: 'completed'}]), '');
});

test('the panel renders one labelled Cancel button per open job, disabled while in flight', () => {
  const html = jobsPanel(jobs, {'an-run': {pending: true}});
  assert.equal((html.match(/data-cancel="/g) || []).length, 2);
  assert.match(html, /aria-label="Cancel analysis an-req"/);
  assert.match(html, /o\/r @ main on h1/);
  assert.match(html, /data-cancel="an-run" aria-label="Cancel analysis an-run" disabled aria-busy="true">Cancelling…/);
  assert.doesNotMatch(html, /data-cancel="an-req"[^>]*disabled/);
});

test('error text and job fields are escaped', () => {
  const html = jobsPanel([{analysis_id: 'an-<b>', state: 'requested', host_id: '<img src=x onerror=alert(1)>'}],
    {'an-<b>': {error: '<script>alert(1)</script>'}});
  assert.doesNotMatch(html, /<script>|<img|<b>/);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
});

test('declining the confirm sends nothing', async () => {
  let called = 0;
  const client = {cancelArchitectureAnalysis: async () => { called++; return {}; }};
  assert.deepEqual(await cancelJob(client, 'an-1', {confirm: () => false}), {cancelled: false});
  assert.deepEqual(await cancelJob(client, 'an-1', {confirm: undefined}), {cancelled: false}); // no confirm: never cancel
  assert.equal(called, 0);
});

test('a confirmed cancel returns the cancelled analysis; a refusal is surfaced with the server text', async () => {
  const asked = [];
  const ok = {cancelArchitectureAnalysis: async id => ({analysis: {analysis_id: id, state: 'cancelled'}})};
  const out = await cancelJob(ok, 'an-1', {confirm: m => { asked.push(m); return true; }});
  assert.deepEqual(out, {cancelled: true, analysis: {analysis_id: 'an-1', state: 'cancelled'}});
  assert.deepEqual(asked, ['Cancel analysis an-1?']);
  const refuse = status => ({cancelArchitectureAnalysis: async () => { throw Object.assign(new Error('operator role required'), {status}); }});
  assert.deepEqual(await cancelJob(refuse(403), 'an-1', {confirm: () => true}),
    {cancelled: false, error: 'Only an operator key can cancel analyses: operator role required'});
  assert.match((await cancelJob(refuse(401), 'an-1', {confirm: () => true})).error, /^Only an operator key/);
  const finished = {cancelArchitectureAnalysis: async () => { throw Object.assign(new Error('analysis already finished'), {status: 409}); }};
  assert.deepEqual(await cancelJob(finished, 'an-1', {confirm: () => true}), {cancelled: false, error: 'Cancel failed: analysis already finished'});
});

test('the client posts to the encoded cancel route', async () => {
  const seen = [];
  const fetchImpl = async (url, init) => { seen.push([url, init.method, init.body]); return new Response('{"analysis":{}}', {status: 200, headers: {'Content-Type': 'application/json'}}); };
  const c = new HerdrClient('http://cp', {fetchImpl});
  await c.cancelArchitectureAnalysis('an/1');
  assert.equal(seen[0][0], 'http://cp/api/v1/architecture/analyses/an%2F1/cancel');
  assert.equal(seen[0][1], 'POST');
  assert.equal(seen[0][2], '{}');
});
