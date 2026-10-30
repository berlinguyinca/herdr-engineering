import test from 'node:test';
import assert from 'node:assert/strict';
import { HerdrClient, ApiError } from './client.js';
import { renderReportDiff } from '../artifacts-view.js';

const response = body => ({ ok: true, status: 200, text: async () => JSON.stringify(body) });

test('report metadata and differences always identify immutable versions', async () => {
  const calls = [];
  const client = new HerdrClient('http://localhost:1234/', { fetchImpl: async (url, options) => {
    calls.push({ url, options }); return response({ version_id: 'version:2' });
  } });
  await client.artifactReport('artifact/one', 'version:2');
  await client.artifactReportDiff('artifact/one', 'version:2', 'version:1');
  await client.artifactReportFeedback('artifact/one', 'version:2');
  assert.deepEqual(calls.map(call => call.url), [
    'http://localhost:1234/api/v1/artifacts/artifact%2Fone/versions/version%3A2/report',
    'http://localhost:1234/api/v1/artifacts/artifact%2Fone/versions/version%3A2/diff?base_version_id=version%3A1',
    'http://localhost:1234/api/v1/artifacts/artifact%2Fone/versions/version%3A2/feedback',
  ]);
});

test('feedback and checks preserve the exact version and derive no author identity', async () => {
  const calls = [];
  const client = new HerdrClient('', { fetchImpl: async (url, options) => {
    calls.push({ url, options, body: JSON.parse(options.body) }); return response({ ok: true });
  } });
  const feedback = { target_kind: 'finding', target_id: 'latency', body: 'Denominator is unclear.' };
  const check = { stage: 'evidence_checked', status: 'passed', notes: 'Compared source rows.', evidence_refs: ['evidence:timing'] };
  await client.addArtifactReportFeedback('a', 'v1', feedback);
  await client.recordArtifactReportCheck('a', 'v1', check);
  assert.equal(calls[0].url, '/api/v1/artifacts/a/versions/v1/feedback');
  assert.equal(calls[1].url, '/api/v1/artifacts/a/versions/v1/checks');
  assert.deepEqual(calls[0].body, feedback);
  assert.deepEqual(calls[1].body, check);
  assert.ok(calls.every(call => call.options.method === 'POST'));
  assert.ok(calls.every(call => !('actor' in call.body) && !('author_id' in call.body)));
});

test('preview and explicit asset download retain authentication and abort signals outside URLs', async () => {
  const calls = [];
  const controller = new AbortController();
  const binary = new Blob(['evidence'], { type: 'text/csv' });
  const client = new HerdrClient('', { headers: { 'X-API-Key': 'test-key' }, fetchImpl: async function(url, options) {
    assert.equal(this, undefined);
    calls.push({ url, options });
    return { ok: true, status: 200, text: async () => '<html>preview</html>', blob: async () => binary };
  } });
  assert.equal(await client.artifactReportPreview('a', 'v1', { signal: controller.signal }), '<html>preview</html>');
  assert.equal(await client.downloadArtifactReportAsset('a', 'v1', 'data/id', { signal: controller.signal }), binary);
  assert.equal(client.artifactReportAssetUrl('a', 'v1', 'data/id'), '/api/v1/artifacts/a/versions/v1/assets/data%2Fid');
  for (const call of calls) {
    assert.equal(call.options.headers['X-API-Key'], 'test-key');
    assert.equal(call.options.signal, controller.signal);
    assert.ok(!call.url.includes('test-key'));
  }
});

test('report payload failures remain visible structured errors', async () => {
  const client = new HerdrClient('', { fetchImpl: async () => ({
    ok: false, status: 403, text: async () => '{"error":"Permission denied"}',
    headers: { get: () => 'request-42' },
  }) });
  await assert.rejects(() => client.artifactReportPreview('a', 'v'), error =>
    error instanceof ApiError && error.status === 403 && error.message === 'Permission denied');
  await assert.rejects(() => client.downloadArtifactReportAsset('a', 'v', 'asset'), error =>
    error instanceof ApiError && error.status === 403);
});

test('JSON requests use the same configured authentication as asset requests', async () => {
  const client = new HerdrClient('', { headers: { 'X-API-Key': 'test-key' }, fetchImpl: async (_url, options) => {
    assert.equal(options.headers['X-API-Key'], 'test-key');
    assert.equal(options.headers.Accept, 'application/json');
    return response({ report_descriptor: {} });
  } });
  await client.artifactReport('a', 'v');
});

test('version history can omit descriptors and feedback pages remain version pinned', async () => {
  const urls = [];
  const client = new HerdrClient('', { fetchImpl: async url => { urls.push(url); return response({}); } });
  await client.artifactVersions('a', { summary: true });
  await client.artifactReportFeedback('a', 'v2', { limit: 25, offset: 100 });
  assert.deepEqual(urls, ['/api/v1/artifacts/a/versions?summary=true',
    '/api/v1/artifacts/a/versions/v2/feedback?limit=25&offset=100']);
});

test('bounded differences preserve complete category counts and disclose omitted finding changes', () => {
  const rendered = renderReportDiff({ changed: true, change_count: 202, detail_limit: 200, truncated: true,
    categories: {
      data: { changed: true, counts: { added: 0, removed: 0, modified: 201 }, truncated: true,
        added: [], removed: [], modified: Array.from({ length: 200 }, (_, i) => ({ path: `/evidence/${i}` })) },
      analysis: { changed: false, counts: { added: 0, removed: 0, modified: 0 }, added: [], removed: [], modified: [] },
      editorial: { changed: true, counts: { added: 0, removed: 0, modified: 1 }, truncated: true,
        added: [], removed: [], modified: [] },
      presentation: { changed: false, counts: { added: 0, removed: 0, modified: 0 }, added: [], removed: [], modified: [] },
    } }, 'v1', 'v2');
  assert.match(rendered, /Total changes: 202/);
  assert.match(rendered, /detail limit: 200/);
  const editorial = rendered.split('data-diff-category="editorial"')[1].split('</article>')[0];
  assert.match(editorial, /1 modified/);
  assert.match(editorial, /omitted/);
  assert.match(rendered, /complete descriptors/);
});

test('context lookup and link reads encode ids and send only the named filters', async () => {
  const calls = [];
  const client = new HerdrClient('', { fetchImpl: async url => { calls.push(url); return response({ artifacts: [], links: [] }); } });
  await client.artifactsForContext('issue', 'org/repo#7', { q: 'plot', limit: 25 });
  await client.artifactsForContext('room', 'r1');
  await client.artifactLinks('a/1');
  assert.deepEqual(calls, [
    '/api/v1/artifacts/for/issue/org%2Frepo%237?q=plot&limit=25',
    '/api/v1/artifacts/for/room/r1',
    '/api/v1/artifacts/a%2F1/links',
  ]);
});
