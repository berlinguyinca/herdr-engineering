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
