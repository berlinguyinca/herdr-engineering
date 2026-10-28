import test from 'node:test';
import assert from 'node:assert/strict';
import { renderMarkdown } from '../conversation-markdown.js';
import { candidateUrls, createFabricUrlResolver, installFabricLinkActions } from '../fabric-urls.js';

const resolved = (original, canonical, extra = {}) => ({ original, canonical, status: 'resolved', service: 'web', host: '127.0.0.1', ...extra });
const urls = (table) => ({ resolve: (u) => table[u] ?? null });

test('resolved bare URL becomes a clickable canonical link that keeps the original', () => {
  const o = 'http://localhost:3188/benchmarks?x=1&y=%20#f';
  const html = renderMarkdown(`see ${o} now`, { urls: urls({ [o]: resolved(o, 'https://dev.lan:3188/benchmarks?x=1&y=%20#f') }) });
  assert.ok(html.includes('href="https://dev.lan:3188/benchmarks?x=1&amp;y=%20#f"'));
  assert.ok(html.includes(`data-original="${o.replace(/&/g, '&amp;')}"`));
  assert.ok(html.includes('Original: ') && html.includes('Service: web') && html.includes('Host: 127.0.0.1'));
  assert.equal((html.match(/data-copy="/g) || []).length, 2);
  assert.ok(html.includes('rel="noopener noreferrer nofollow"'));
});

test('markdown link keeps its label and points at the canonical URL', () => {
  const o = 'http://localhost:3188/a';
  const html = renderMarkdown(`[dashboard](${o})`, { urls: urls({ [o]: resolved(o, 'https://dev.lan:3188/a') }) });
  assert.ok(html.includes('href="https://dev.lan:3188/a"') && html.includes('>dashboard</a>'));
});

test('unresolved loopback URL is shown with the reason, not as a dead link', () => {
  const o = 'http://localhost:3188/x';
  const html = renderMarkdown(o, { urls: urls({ [o]: { original: o, canonical: null, status: 'unhealthy', reason: 'service unreachable: refused' } }) });
  assert.ok(!html.includes('<a ') && html.includes('md-link-unreachable') && html.includes('service unreachable: refused'));
});

test('a resolver cannot inject an unsafe canonical target', () => {
  const o = 'http://localhost:3188/x';
  for (const bad of ['javascript:alert(1)', 'data:text/html,x', '//evil.test', 'https://u:p@evil.test/']) {
    const html = renderMarkdown(o, { urls: urls({ [o]: resolved(o, bad) }) });
    assert.ok(!html.includes('href="javascript') && !html.includes('href="data') && !html.includes('evil.test"'), bad);
  }
});

test('external URLs, code spans and fences are never rewritten', () => {
  const o = 'http://localhost:3188/x';
  const t = urls({ [o]: resolved(o, 'https://dev.lan:3188/x'), 'https://example.com/': { status: 'not_loopback', canonical: null } });
  assert.ok(renderMarkdown('https://example.com/', { urls: t }).includes('href="https://example.com/"'));
  assert.ok(!renderMarkdown(`\`${o}\``, { urls: t }).includes('dev.lan'));
  assert.ok(!renderMarkdown(`\`\`\`\ncurl ${o}\n\`\`\``, { urls: t }).includes('dev.lan'));
});

test('without a resolver output is unchanged', () => {
  assert.equal(renderMarkdown('http://localhost:3188/x'), renderMarkdown('http://localhost:3188/x', { urls: null }));
});

test('candidateUrls finds loopback and .lan URLs only, trimming sentence punctuation', () => {
  assert.deepEqual(candidateUrls('go http://localhost:3188/a, or https://example.com/ and http://[::1]:80/z. Also http://dev.lan:9/q'),
    ['http://localhost:3188/a', 'http://[::1]:80/z', 'http://dev.lan:9/q']);
});

test('resolver batches, caches, and notifies once results arrive', async () => {
  const calls = []; let changed = 0;
  const client = { resolveFabricUrls: async (u, m) => { calls.push([u, m]); return { resolutions: u.map((x) => resolved(x, 'https://dev.lan:1/')) }; } };
  const r = createFabricUrlResolver(client, { sourceMachine: () => 'bender', onChange: () => { changed++; } });
  assert.equal(r.resolver.resolve('http://localhost:1/'), null);
  r.prefetch('x http://localhost:2/ y');
  await r.flush();
  assert.equal(calls.length, 1); assert.deepEqual(calls[0][0].sort(), ['http://localhost:1/', 'http://localhost:2/']); assert.equal(calls[0][1], 'bender');
  assert.equal(r.resolver.resolve('http://localhost:1/').canonical, 'https://dev.lan:1/');
  assert.equal(changed, 1); assert.equal(calls.length, 1);
});

test('gateway failure degrades to unavailable without throwing', async () => {
  const r = createFabricUrlResolver({ resolveFabricUrls: async () => { throw new Error('down'); } });
  r.prefetch('http://localhost:1/'); await r.flush();
  assert.equal(r.resolver.resolve('http://localhost:1/').status, 'unavailable');
});

test('copy buttons write the exact stored value', async () => {
  const written = []; let handler;
  const root = { contains: () => true, addEventListener: (_, h) => { handler = h; }, removeEventListener() {} };
  installFabricLinkActions(root, { clipboard: { writeText: async (v) => { written.push(v); } } });
  const button = { dataset: { copy: 'http://localhost:3188/x' } };
  await handler({ target: { closest: () => button }, preventDefault() {} });
  assert.deepEqual(written, ['http://localhost:3188/x']);
});

test('fabric names from the gateway extend link detection to the public fleet name and subdomains', async () => {
  const { candidateUrls, setFabricNames, createFabricUrlResolver } = await import('../fabric-urls.js');
  const text = 'open https://svc.local.metabolomics.us:3188/x and https://local.metabolomics.us:4000 and https://evil.example.com:3188';
  try {
    assert.deepEqual(candidateUrls(text), []);
    setFabricNames(['local.metabolomics.us', 'bad name!', 42]);
    assert.deepEqual(candidateUrls(text), ['https://svc.local.metabolomics.us:3188/x', 'https://local.metabolomics.us:4000']);
    assert.deepEqual(candidateUrls('https://notlocal.metabolomics.us:1'), []);   // a suffix match must start at a label boundary

    setFabricNames([]);
    let changed = 0;
    createFabricUrlResolver({ getFabricNames: async () => ({ available: true, names: ['local.metabolomics.us'] }),
      resolveFabricUrls: async () => ({ resolutions: [] }) }, { onChange: () => { changed++; } });
    await new Promise((r) => setTimeout(r, 20));
    assert.equal(changed, 1);
    assert.equal(candidateUrls(text).length, 2);
  } finally { setFabricNames([]); }
});

test('a gateway without names leaves detection unchanged', async () => {
  const { candidateUrls, createFabricUrlResolver } = await import('../fabric-urls.js');
  createFabricUrlResolver({ getFabricNames: async () => { throw new Error('down'); }, resolveFabricUrls: async () => ({}) });
  await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(candidateUrls('http://localhost:3188/a'), ['http://localhost:3188/a']);
});
