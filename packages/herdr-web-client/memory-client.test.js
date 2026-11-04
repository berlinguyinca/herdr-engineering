import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ApiError, HerdrClient } from './client.js';

const base = 'https://herdr.example';
const bank = 'project/facts ?#%';
const id = 'note/one ?#%';
const bankPath = '/api/v1/assistant-memory/operator/v1/default/banks/project%2Ffacts%20%3F%23%25';
const documentPath = `${bankPath}/documents/note%2Fone%20%3F%23%25`;

function capture(body = {}, status = 200) {
  const calls = [];
  const client = new HerdrClient(`${base}/`, {
    user: 'memory-user', tenant: 'memory-tenant',
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return new Response(JSON.stringify(body), {
        status, headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'memory-request' },
      });
    },
  });
  return { client, calls };
}

function assertRequest(calls, path, method, body, signal) {
  assert.equal(calls.length, 1);
  const { url, options } = calls[0];
  assert.equal(url, `${base}${path}`);
  assert.equal(options.method, method);
  if (signal) assert.equal(options.signal, signal);
  else assert.ok(options.signal instanceof AbortSignal);
  assert.deepEqual(options.headers, {
    Accept: 'application/json',
    'X-Herdr-User': 'memory-user',
    'X-Herdr-Tenant': 'memory-tenant',
    ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
  });
  if (body === undefined) assert.equal(options.body, undefined);
  else assert.deepEqual(JSON.parse(options.body), body);
}

const reads = [
  {
    name: 'listMemoryDomains', args: [], path: '/api/v1/assistant-memory/domains',
    response: { domains: [] },
  },
  {
    name: 'listMemoryDocuments', args: [bank], path: `${bankPath}/documents?limit=20&offset=0`,
    response: { documents: [], total: 0, limit: 20, offset: 0 },
  },
  {
    name: 'getMemoryDocument', args: [bank, id], path: documentPath,
    response: { document_id: id, text: 'Shared fact', metadata: { revision: 2 } },
  },
  {
    name: 'getMemoryHistory', args: [bank, id], path: `${documentPath}/history`,
    response: { history: [{ revision: 2, operation: 'UPDATE', capture_kind: 'fact' }] },
  },
  {
    name: 'getMemoryOperation', args: [bank, 'op/one ?#%'],
    path: `${bankPath}/operations/op%2Fone%20%3F%23%25`,
    response: { operation_id: 'op/one ?#%', status: 'pending' },
  },
];

for (const { name, args, path, response } of reads) {
  test(`${name} uses the encoded operator route and forwards identity and signal`, async () => {
    const { client, calls } = capture(response);
    const signal = new AbortController().signal;
    assert.deepEqual(await client[name](...args, { signal }), response);
    assertRequest(calls, path, 'GET', undefined, signal);
  });

  test(`${name} permits omitted options`, async () => {
    const { client, calls } = capture(response);
    assert.deepEqual(await client[name](...args), response);
    assertRequest(calls, path, 'GET');
  });
}

test('listMemoryDocuments serializes pagination without leaking options into the query', async () => {
  const { client, calls } = capture();
  const signal = new AbortController().signal;
  await client.listMemoryDocuments(bank, { limit: 7, offset: 14, signal, token: 'not-a-credential' });
  assertRequest(calls, `${bankPath}/documents?limit=7&offset=14`, 'GET', undefined, signal);
});

for (const revision of [undefined, 0, 7]) {
  test(`saveMemoryDocument preserves revision ${revision} and forces explicit save without mutating metadata`, async () => {
    const receipt = { operation_id: 'op-1', status: 'pending' };
    const { client, calls } = capture(receipt, 202);
    const signal = new AbortController().signal;
    const metadata = Object.freeze({ source: 'operator', explicit_save: false });
    const document = Object.freeze({ document_id: id, content: 'A shared fact', metadata,
      ...(revision === undefined ? {} : { base_revision: revision }) });
    const result = await client.saveMemoryDocument(bank, document, { signal });
    assert.deepEqual(result, receipt);
    assert.equal(result._requestMeta.requestId, 'memory-request');
    assertRequest(calls, `${bankPath}/memories`, 'POST', {
      items: [{ document_id: id, content: 'A shared fact',
        metadata: { source: 'operator', explicit_save: true },
        ...(revision === undefined ? {} : { base_revision: revision }) }],
      async: true,
    }, signal);
    assert.deepEqual(metadata, { source: 'operator', explicit_save: false });
  });
}

test('saveMemoryDocument defaults metadata and options', async () => {
  const { client, calls } = capture({}, 202);
  await client.saveMemoryDocument(bank, { document_id: id, content: 'New fact' });
  assertRequest(calls, `${bankPath}/memories`, 'POST', {
    items: [{ document_id: id, content: 'New fact', metadata: { explicit_save: true } }], async: true,
  });
});

for (const limit of [undefined, 4]) {
  test(`recallMemoryDocuments POSTs the query with ${limit ?? 'default'} limit`, async () => {
    const response = { results: [{ document_id: id, text: 'A fact', metadata: {} }] };
    const { client, calls } = capture(response);
    const query = 'facts about / project? #memory & café';
    const signal = limit === undefined ? undefined : new AbortController().signal;
    const result = limit === undefined
      ? await client.recallMemoryDocuments(bank, query)
      : await client.recallMemoryDocuments(bank, query, { limit, signal });
    assert.deepEqual(result, response);
    assertRequest(calls, `${bankPath}/memories/recall`, 'POST', { query, limit: limit ?? 100 }, signal);
  });
}

for (const revision of [undefined, 0, 7]) {
  test(`deleteMemoryDocument sends an optional revision ${revision} and returns the 202 receipt`, async () => {
    const receipt = { operation_id: 'delete-1', status: 'pending' };
    const { client, calls } = capture(receipt, 202);
    const signal = new AbortController().signal;
    assert.deepEqual(await client.deleteMemoryDocument(bank, id, { base_revision: revision, signal }), receipt);
    assertRequest(calls, documentPath, 'DELETE', revision === undefined ? {} : { base_revision: revision }, signal);
  });
}

test('deleteMemoryDocument permits omitted options', async () => {
  const { client, calls } = capture({}, 202);
  await client.deleteMemoryDocument(bank, id);
  assertRequest(calls, documentPath, 'DELETE', {});
});

for (const { name, args } of [
  ...reads,
  { name: 'saveMemoryDocument', args: [bank, { document_id: id, content: 'Fact', base_revision: 1 }] },
  { name: 'recallMemoryDocuments', args: [bank, 'query'] },
  { name: 'deleteMemoryDocument', args: [bank, id] },
]) {
  for (const status of [404, 409]) {
    test(`${name} preserves structured HTTP ${status} errors`, async () => {
      const body = { error: status === 404 ? 'document unavailable' : 'revision conflict', revision: 2 };
      const { client, calls } = capture(body, status);
      await assert.rejects(async () => client[name](...args), error => {
        assert.ok(error instanceof ApiError);
        assert.equal(error.status, status);
        assert.equal(error.message, body.error);
        assert.deepEqual(error.body, body);
        assert.equal(error.requestId, 'memory-request');
        return true;
      });
      assert.equal(calls.length, 1);
    });
  }
}

for (const document_id of ['.', '..']) {
  test(`saveMemoryDocument rejects URL-normalized document ID ${document_id} before sending`, () => {
    const { client, calls } = capture();
    assert.throws(() => client.saveMemoryDocument('fact', { document_id, content: 'note' }), /Document ID/);
    assert.equal(calls.length, 0);
  });
}
