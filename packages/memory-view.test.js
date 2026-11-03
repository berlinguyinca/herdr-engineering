import test from 'node:test';
import assert from 'node:assert/strict';
import { searchParams, classify, renderResults, renderDrawer, renderStatus, renderFilters, provenanceLinks, mountMemory } from './memory-view.js';

// Mode: MOCKED client. Nothing here talks to a control plane or herdr-memory.
const HOSTILE = '<img src=x onerror=alert(1)>';
const mem = (over = {}) => ({ memory_id: 'm1', bank_id: 'alpha-facts', scope: 'project', project: 'alpha', revision: 2, kind: 'fact', status: 'verified',
  text: 'Use the staging queue', untrusted: true, provenance: { session_id: 's-1', mission_id: 'mis-1', pr_ref: 'o/r#12', commit_sha: 'abc123', evidence_refs: ['handoff:h-9', 'file:x'] }, ...over });

test('searchParams keeps only filled fields, trims, and pages', () => {
  assert.equal(searchParams({ q: ' hi ', scope: '', host: 'bender' }, 40).toString(), 'q=hi&host=bender&limit=20&offset=40');
});

test('classify separates forbidden, unavailable, disabled and other failures', () => {
  assert.equal(classify({ status: 403 }).phase, 'forbidden');
  assert.equal(classify({ status: 503 }).phase, 'unavailable');
  assert.equal(classify({ status: 404 }).phase, 'disabled');
  assert.equal(classify({ message: 'boom' }).message, 'boom');
});

test('every result state has distinct text and no invented rows', () => {
  assert.match(renderResults({ phase: 'idle' }), /Nothing is listed until you search/);
  assert.match(renderResults({ phase: 'loading' }), /aria-busy/);
  assert.match(renderResults({ phase: 'forbidden' }), /No access/);
  assert.match(renderResults({ phase: 'unavailable' }), /Memory service unavailable/);
  const none = renderResults({ phase: 'ok', results: [], offset: 0 });
  assert.match(none, /No matching memories/); assert.doesNotMatch(none, /data-memory=/);
  assert.match(renderResults({ phase: 'ok', results: [mem()], degraded: true, has_more: true, offset: 0 }), /Degraded/);
});

test('rows label status as text, escape hostile fields and mark data untrusted', () => {
  const html = renderResults({ phase: 'ok', results: [mem({ text: HOSTILE, kind: HOSTILE, memory_id: HOSTILE })], offset: 0, has_more: false });
  assert.doesNotMatch(html, /<img/i);
  assert.match(html, /status: verified/); assert.match(html, /untrusted data/);
});

test('paging buttons reflect offset and has_more', () => {
  const first = renderResults({ phase: 'ok', results: [mem()], offset: 0, has_more: true });
  assert.match(first, /data-mem-page="prev" disabled/); assert.doesNotMatch(first, /data-mem-page="next" disabled/);
  const last = renderResults({ phase: 'ok', results: [mem()], offset: 20, has_more: false });
  assert.match(last, /Results 21–21/); assert.match(last, /data-mem-page="next" disabled/);
});

test('links only for ids present in provenance; commit stays text; no edges invented', () => {
  const links = provenanceLinks(mem());
  assert.deepEqual(links.map((l) => [l.label, l.href]), [['Mission', '#/missions/mis-1'], ['Session', '#/herd/session/s-1'],
    ['Pull request', '#/pr/o/r/12'], ['Commit', null], ['Handoff', '#/handoffs?open=h-9']]);
  assert.deepEqual(provenanceLinks(mem({ provenance: {} })), []);
  assert.match(renderDrawer({ phase: 'ok', memory: mem({ provenance: {} }), retrieval_history: {} }), /No linked sessions/);
});

test('drawer shows revisions only when reported, honest retrieval-history unknown, and escapes', () => {
  const d = renderDrawer({ phase: 'ok', memory: mem({ text: HOSTILE, revisions: null }), retrieval_history: { state: 'unknown', events: [], reason: 'not exposed' } });
  assert.doesNotMatch(d, /<img/i);
  assert.match(d, /Prior revisions were not reported/); assert.match(d, /Unknown: not exposed/);
  const withRevs = renderDrawer({ phase: 'ok', memory: mem({ revisions: [{ revision: 1, status: 'superseded', at: 't', text: 'old' }] }), retrieval_history: {} });
  assert.match(withRevs, /rev 1/); assert.match(withRevs, /status: superseded/);
  assert.match(renderDrawer({ phase: 'notfound' }), /not visible to you/);
});

test('integration status shows UNKNOWN rather than fabricating per host rows', () => {
  const html = renderStatus({ phase: 'ok', data: { healthy: true, contract: 1, outbox_pending: null, integrations: { state: 'unknown', rows: [], reason: 'not reported' } } });
  assert.match(html, /UNKNOWN/); assert.doesNotMatch(html, /<table/);
  assert.match(renderStatus({ phase: 'unavailable' }), /UNKNOWN/);
});

test('filters render every contract filter with labels', () => {
  const html = renderFilters({ q: HOSTILE });
  for (const n of ['q', 'scope', 'project', 'kind', 'status', 'harness', 'host']) assert.match(html, new RegExp(`name="${n}"`));
  assert.doesNotMatch(html, /<img/i);
});

// --- mount against a tiny fake DOM-less root: only what mountMemory touches
function fakeRoot() {
  const nodes = {}; const handlers = {};
  const make = (key) => (nodes[key] ||= { innerHTML: '', hidden: false, focus() {} });
  const root = {
    set innerHTML(_) { /* shell */ }, get innerHTML() { return ''; },
    querySelector(sel) { return sel === '#mem-drawer-title' ? { focus() {} } : make(sel); },
    addEventListener(type, fn) { handlers[type] = fn; },
  };
  return { root, nodes, handlers };
}
const tick = () => new Promise((r) => setTimeout(r, 0));

test('mount: nothing is fetched for results until the person searches; then results, drawer, paging', async () => {
  const calls = [];
  const client = { async exploreMemory(p) { calls.push(['search', p.toString()]); return { results: [mem()], has_more: true, offset: Number(p.get('offset') || 0) }; },
    async getMemoryRecord(id) { calls.push(['get', id]); return { memory: mem(), retrieval_history: { state: 'unknown', events: [] } }; },
    async getMemoryServiceStatus() { return { healthy: true, contract: 1, integrations: { state: 'unknown', rows: [] } }; } };
  const { root, nodes, handlers } = fakeRoot();
  const view = mountMemory(root, client, { query: new URLSearchParams('') });
  await tick();
  assert.deepEqual(calls, []);
  assert.match(nodes['[data-mem-results]'].innerHTML, /Search memory/);
  assert.match(nodes['[data-mem-status]'].innerHTML, /healthy/);
  handlers.submit({ target: { matches: () => true, elements: { q: { value: 'queue' } } }, preventDefault() {} });
  await tick();
  assert.equal(calls[0][1], 'q=queue&limit=20');
  assert.match(nodes['[data-mem-results]'].innerHTML, /Use the staging queue/);
  handlers.click({ target: { closest: (s) => (s === '[data-memory]' ? { dataset: { memory: 'm1' }, focus() {} } : null) } });
  await tick();
  assert.deepEqual(calls[1], ['get', 'm1']);
  assert.equal(nodes['[data-mem-drawer]'].hidden, false);
  handlers.click({ target: { closest: (s) => (s === '[data-mem-page]' ? { disabled: false, dataset: { memPage: 'next' } } : null) } });
  await tick();
  assert.match(calls[2][1], /offset=20/);
  view.dispose();
});

test('mount: 503 shows the unavailable state, never stale results', async () => {
  const client = { async exploreMemory() { throw Object.assign(new Error('x'), { status: 503 }); }, async getMemoryServiceStatus() { throw Object.assign(new Error('x'), { status: 503 }); }, async getMemoryRecord() { return {}; } };
  const { root, nodes, handlers } = fakeRoot();
  mountMemory(root, client, { query: new URLSearchParams('q=hello') });
  await tick();
  assert.match(nodes['[data-mem-results]'].innerHTML, /Memory service unavailable/);
  assert.doesNotMatch(nodes['[data-mem-results]'].innerHTML, /data-memory=/);
  assert.match(nodes['[data-mem-status]'].innerHTML, /UNKNOWN/);
  assert.ok(handlers.keydown);
});
