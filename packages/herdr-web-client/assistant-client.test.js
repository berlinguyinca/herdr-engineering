import assert from 'node:assert/strict';
import { test } from 'node:test';
import { HerdrClient } from './client.js';

function capture() {
  const calls = [];
  const fetchImpl = async (url, opts) => {
    calls.push({ url, opts });
    return { ok: true, status: 200, headers: { get: () => null }, text: async () => '{}' };
  };
  return { calls, fetchImpl };
}

test('assistant client sends identity headers on every request', async () => {
  const { calls, fetchImpl } = capture();
  const c = new HerdrClient('', { fetchImpl, user: 'u1', tenant: 't1' });
  await c.listAssistants();
  const h = calls[0].opts.headers;
  assert.equal(h['X-Herdr-User'], 'u1');
  assert.equal(h['X-Herdr-Tenant'], 't1');
});

test('createAssistant POSTs to /api/v1/assistants', async () => {
  const { calls, fetchImpl } = capture();
  const c = new HerdrClient('', { fetchImpl });
  await c.createAssistant({ name: 'Helper' });
  assert.equal(calls[0].url, '/api/v1/assistants');
  assert.equal(calls[0].opts.method, 'POST');
  assert.equal(JSON.parse(calls[0].opts.body).name, 'Helper');
});

test('sendAssistantMessage posts to the thread messages endpoint', async () => {
  const { calls, fetchImpl } = capture();
  const c = new HerdrClient('', { fetchImpl });
  await c.sendAssistantMessage('asst1', 'thr1', 'hello');
  assert.equal(calls[0].url, '/api/v1/assistants/asst1/threads/thr1/messages');
  assert.equal(calls[0].opts.method, 'POST');
  assert.equal(JSON.parse(calls[0].opts.body).content, 'hello');
});

test('decideApproval posts decision to the approval endpoint', async () => {
  const { calls, fetchImpl } = capture();
  const c = new HerdrClient('', { fetchImpl });
  await c.decideApproval('appr1', 'approved');
  assert.equal(calls[0].url, '/api/v1/assistant-approvals/appr1/decision');
  assert.equal(JSON.parse(calls[0].opts.body).decision, 'approved');
});

test('forgetMemory issues DELETE', async () => {
  const { calls, fetchImpl } = capture();
  const c = new HerdrClient('', { fetchImpl });
  await c.forgetMemory('mem1');
  assert.equal(calls[0].url, '/api/v1/assistant-memory/mem1');
  assert.equal(calls[0].opts.method, 'DELETE');
});

test('correctMemory issues PATCH to assistant-memory', async () => {
  const { calls, fetchImpl } = capture();
  const c = new HerdrClient('', { fetchImpl });
  await c.correctMemory('mem1', 'prefers tea');
  assert.equal(calls[0].url, '/api/v1/assistant-memory/mem1');
  assert.equal(calls[0].opts.method, 'PATCH');
  assert.equal(JSON.parse(calls[0].opts.body).content, 'prefers tea');
});

test('createRoutine POSTs to /api/v1/assistant-routines', async () => {
  const { calls, fetchImpl } = capture();
  const c = new HerdrClient('', { fetchImpl });
  await c.createRoutine({ name: 'Briefing', trigger_type: 'interval', trigger_spec: { every: 'hours', amount: 1 } });
  assert.equal(calls[0].url, '/api/v1/assistant-routines');
  assert.equal(calls[0].opts.method, 'POST');
  assert.equal(JSON.parse(calls[0].opts.body).name, 'Briefing');
});

test('triggerRoutine POSTs to the trigger endpoint', async () => {
  const { calls, fetchImpl } = capture();
  const c = new HerdrClient('', { fetchImpl });
  await c.triggerRoutine('rout1');
  assert.equal(calls[0].url, '/api/v1/assistant-routines/rout1/trigger');
  assert.equal(calls[0].opts.method, 'POST');
});

test('archiveRoutine issues DELETE', async () => {
  const { calls, fetchImpl } = capture();
  const c = new HerdrClient('', { fetchImpl });
  await c.archiveRoutine('rout1');
  assert.equal(calls[0].url, '/api/v1/assistant-routines/rout1');
  assert.equal(calls[0].opts.method, 'DELETE');
});

test('registerConnector POSTs to /api/v1/assistant-connectors', async () => {
  const { calls, fetchImpl } = capture();
  const c = new HerdrClient('', { fetchImpl });
  await c.registerConnector({ name: 'W', base_url: 'https://w.example.com', auth: { token: 'x' } });
  assert.equal(calls[0].url, '/api/v1/assistant-connectors');
  assert.equal(calls[0].opts.method, 'POST');
  assert.equal(JSON.parse(calls[0].opts.body).name, 'W');
});

test('connectorRequest POSTs to the request endpoint', async () => {
  const { calls, fetchImpl } = capture();
  const c = new HerdrClient('', { fetchImpl });
  await c.connectorRequest('conn1', { path: '/api' });
  assert.equal(calls[0].url, '/api/v1/assistant-connectors/conn1/request');
  assert.equal(calls[0].opts.method, 'POST');
});

test('delegate POSTs to /api/v1/assistant-delegations', async () => {
  const { calls, fetchImpl } = capture();
  const c = new HerdrClient('', { fetchImpl });
  await c.delegate({ task: 'T' });
  assert.equal(calls[0].url, '/api/v1/assistant-delegations');
  assert.equal(calls[0].opts.method, 'POST');
});

test('registerDelegationArtifact POSTs to the artifact endpoint', async () => {
  const { calls, fetchImpl } = capture();
  const c = new HerdrClient('', { fetchImpl });
  await c.registerDelegationArtifact('dlg1', 'artifact://mission/MSN-1');
  assert.equal(calls[0].url, '/api/v1/assistant-delegations/dlg1/artifact');
  assert.equal(JSON.parse(calls[0].opts.body).artifact_ref, 'artifact://mission/MSN-1');
});

test('testConnector posts the assistant to the gated test route (not the bare request route)', async () => {
  const { calls, fetchImpl } = capture();
  const c = new HerdrClient('', { fetchImpl });
  await c.testConnector('conn 1', { assistant_id: 'asst1' });
  assert.equal(calls[0].url, '/api/v1/assistant-connectors/conn%201/test');
  assert.equal(calls[0].opts.method, 'POST');
  assert.equal(JSON.parse(calls[0].opts.body).assistant_id, 'asst1');
});

test('proposal, execute and notification calls hit the documented routes', async () => {
  const { calls, fetchImpl } = capture();
  const c = new HerdrClient('', { fetchImpl });
  await c.listProposals('approved');
  await c.previewProposal('p1');
  await c.decideProposal('p1', 'rejected');
  await c.executeProposal('p1');
  await c.executeApproval('a1');
  await c.listNotifications('ready');
  await c.ackNotification('n1');
  assert.deepEqual(calls.map(x => [x.opts.method, x.url]), [
    ['GET', '/api/v1/assistant-proposals?status=approved'],
    ['GET', '/api/v1/assistant-proposals/p1/preview'],
    ['POST', '/api/v1/assistant-proposals/p1/decision'],
    ['POST', '/api/v1/assistant-proposals/p1/execute'],
    ['POST', '/api/v1/assistant-approvals/a1/execute'],
    ['GET', '/api/v1/assistant-notifications?state=ready'],
    ['POST', '/api/v1/assistant-notifications/n1/ack'],
  ]);
  assert.equal(JSON.parse(calls[2].opts.body).decision, 'rejected');
});

test('assistant apps client: exact URL, verb and JSON body for every method', async () => {
  const { calls, fetchImpl } = capture();
  const c = new HerdrClient('', { fetchImpl });
  await c.listApps();
  await c.saveAppClient('google', 'cid.apps', 's3cret');
  await c.connectApp('google');
  await c.disconnectApp('google');
  await c.listCalendars('google');
  await c.setBlockedCalendars('google', ['a@x.org', 'b@x.org']);
  await c.listDrafts('google');
  await c.discardDraft('google', 'r-1');
  await c.listReminders();
  await c.cancelReminder('rem_1');
  const seen = calls.map((x) => [x.opts.method, x.url, x.opts.body === undefined ? undefined : JSON.parse(x.opts.body)]);
  assert.deepEqual(seen, [
    ['GET', '/api/v1/assistant-apps', undefined],
    ['PUT', '/api/v1/assistant-apps/google/client', { client_id: 'cid.apps', client_secret: 's3cret' }],
    ['POST', '/api/v1/assistant-apps/google/connect', {}],
    ['DELETE', '/api/v1/assistant-apps/google', undefined],
    ['GET', '/api/v1/assistant-apps/google/calendars', undefined],
    ['PUT', '/api/v1/assistant-apps/google/blocked-calendars', { ids: ['a@x.org', 'b@x.org'] }],
    ['GET', '/api/v1/assistant-apps/google/drafts', undefined],
    ['DELETE', '/api/v1/assistant-apps/google/drafts/r-1', undefined],
    ['GET', '/api/v1/assistant-reminders', undefined],
    ['DELETE', '/api/v1/assistant-reminders/rem_1', undefined],
  ]);
  assert.equal(calls[1].opts.headers['Content-Type'], 'application/json');
});

test('assistant apps client: ids are path-encoded', async () => {
  const { calls, fetchImpl } = capture();
  const c = new HerdrClient('', { fetchImpl });
  await c.discardDraft('google', 'a/b');
  assert.equal(calls[0].url, '/api/v1/assistant-apps/google/drafts/a%2Fb');
});
