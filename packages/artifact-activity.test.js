import test from 'node:test';
import assert from 'node:assert/strict';
import { renderHistoryRow, renderAttempt, renderDiffFiles, mountActivity } from './artifact-activity.js';

const IMG = '<img src=x onerror=alert(1)>';
const assertInert = html => {
  assert.doesNotMatch(html, /<img/i);
  assert.doesNotMatch(html, /<script/i);
  assert.doesNotMatch(html, /<(iframe|frame|object|embed)/i);
  // real handler attributes only: strip quoted values and text nodes, where escaped payloads legitimately sit
  assert.doesNotMatch(html.replace(/"[^"]*"/g, '""').replace(/>[^<]*</g, '><'), /\son[a-z]+=/i);
};
const tick = () => new Promise(resolve => setTimeout(resolve, 0));

test('a history row shows number, author, message and parent', () => {
  const html = renderHistoryRow({ version_id: 'v2', version_number: 2, created_by: 'agent:Alpha', message: 'fix typo', parent_version_ids: ['v1'] }, {});
  assert.match(html, /v2/);
  assert.match(html, /agent:Alpha/);
  assert.match(html, /fix typo/);
  assert.match(html, /v1/);
  assert.doesNotMatch(html, /data-version-merge/);
  assert.match(html, /data-activity-action="select"[^>]*data-version-id="v2"/);
});

test('a merge version shows both parents and the merge badge', () => {
  const html = renderHistoryRow({ version_id: 'v4', version_number: 4, parent_version_ids: ['v2', 'v3'] }, {});
  assert.match(html, /data-version-merge/);
  assert.match(html, /v2/);
  assert.match(html, /v3/);
});

test('hostile author, message and parent render inert', () => {
  assertInert(renderHistoryRow({ version_id: IMG, version_number: IMG, created_by: IMG, message: IMG, parent_version_ids: [IMG, IMG] }, {}));
});

test('an attempt shows outcome, agent, conflict names and kinds, reason and time', () => {
  const html = renderAttempt({ ticket_id: 't1', outcome: 'conflict', actor_id: 'agent-9', conflict_names: [{ name: 'a.txt', kind: 'both-modified' }, 'b.txt'], reason: 'stale base', received_at: '2026-11-02T10:00:00Z' });
  for (const text of ['conflict', 'agent-9', 'a.txt', 'both-modified', 'b.txt', 'stale base', '2026-11-02']) assert.match(html, new RegExp(text));
});

test('a hostile conflict name, reason and agent render inert', () => {
  assertInert(renderAttempt({ ticket_id: IMG, outcome: IMG, actor_id: IMG, conflict_names: [IMG, { name: IMG, kind: IMG }], reason: IMG, received_at: IMG }));
});

test('a binary or oversize file shows the reason and no diff body; a text file gets a diff', () => {
  const html = renderDiffFiles({ files: [
    { name: 'a.bin', change: 'modified', diff: null, reason: 'binary' },
    { name: 'big.txt', change: 'modified', diff: null, reason: 'oversize' },
    { name: 'a.txt', change: 'modified', diff: '--- a\n+++ b\n-old\n+new' },
  ] });
  assert.match(html, /binary/);
  assert.match(html, /oversize/);
  assert.equal(html.match(/<pre/g).length, 1);
  assert.match(html, /hd-diff__add/);
});

test('a hostile file name and diff body render inert', () => {
  assertInert(renderDiffFiles({ files: [{ name: IMG, change: IMG, diff: `+${IMG}` }, { name: IMG, change: 'modified', diff: null, reason: IMG }] }));
});

// ------------------------------------------------------------------ mount

function fakeContainer() {
  const listeners = {};
  let html = '';
  const written = [];
  const lookup = (selector, data) => {
    const attr = selector.match(/^\[data-([a-z-]+)\]$/)?.[1];
    const key = attr?.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    return key && key in data ? { dataset: data, disabled: false } : null;
  };
  const container = {
    isConnected: true, written,
    get innerHTML() { return html; },
    set innerHTML(value) { html = String(value); written.push(html); },
    replaceChildren() { container.innerHTML = ''; },
    querySelector: () => null,
    contains: () => true,
    addEventListener(type, fn) { (listeners[type] ||= new Set()).add(fn); },
    removeEventListener(type, fn) { listeners[type]?.delete(fn); },
    listenerCount: () => Object.values(listeners).reduce((n, set) => n + set.size, 0),
    click(data) { for (const fn of listeners.click || []) fn({ target: { closest: s => lookup(s, data) } }); },
  };
  return container;
}

function fakeClient({ attempts, diff, diffError } = {}) {
  const calls = [];
  return {
    calls,
    count: name => calls.filter(c => c[0] === name).length,
    async artifactCollabActivity(id) { calls.push(['activity', id]); return attempts ?? { attempts: [{ ticket_id: 't1', outcome: 'rejected', actor_id: 'agent-9', conflict_names: [], reason: 'too big', received_at: '2026-11-02T10:00:00Z' }] }; },
    async artifactVersionDiff(id, versionId) { calls.push(['diff', id, versionId]); if (diffError) throw diffError; return diff ?? { version_id: versionId, against: 'v1', files: [{ name: 'a.txt', change: 'modified', diff: '+x' }] }; },
  };
}

const versions = [
  { version_id: 'v2', version_number: 2, created_by: 'me', message: 'second', parent_version_ids: ['v1'] },
  { version_id: 'v1', version_number: 1, created_by: 'me', message: 'first' },
];

test('the mount lists history without any network call until a version or the toggle is used', async () => {
  const container = fakeContainer();
  const client = fakeClient();
  const panel = mountActivity(container, client, 'art1', { versions });
  await tick();
  assert.match(container.innerHTML, /second/);
  assert.equal(client.calls.length, 0);
  panel.dispose();
});

test('the rejected-attempts toggle fetches lazily and only once', async () => {
  const container = fakeContainer();
  const client = fakeClient();
  const panel = mountActivity(container, client, 'art1', { versions });
  container.click({ activityAction: 'toggle-attempts' });
  await tick(); await tick();
  assert.equal(client.count('activity'), 1);
  assert.match(container.innerHTML, /too big/);
  container.click({ activityAction: 'toggle-attempts' });   // hide
  await tick();
  assert.doesNotMatch(container.innerHTML, /too big/);
  container.click({ activityAction: 'toggle-attempts' });   // show again
  await tick(); await tick();
  assert.equal(client.count('activity'), 1);
  assert.match(container.innerHTML, /too big/);
  panel.dispose();
});

test('selecting a version loads its diff and renders the files; a failure is shown, not thrown', async () => {
  const container = fakeContainer();
  const client = fakeClient();
  const panel = mountActivity(container, client, 'art1', { versions });
  container.click({ activityAction: 'select', versionId: 'v2' });
  await tick(); await tick();
  assert.deepEqual(client.calls[0], ['diff', 'art1', 'v2']);
  assert.match(container.innerHTML, /hd-diff/);
  panel.dispose();
  const c2 = fakeContainer();
  const p2 = mountActivity(c2, fakeClient({ diffError: Object.assign(new Error('parent version missing'), { status: 409 }) }), 'art1', { versions });
  c2.click({ activityAction: 'select', versionId: 'v2' });
  await tick(); await tick();
  assert.match(c2.innerHTML, /parent version missing/);
  p2.dispose();
});

test('dispose removes listeners and content', async () => {
  const container = fakeContainer();
  const panel = mountActivity(container, fakeClient(), 'art1', { versions });
  panel.dispose();
  assert.equal(container.innerHTML, '');
  assert.equal(container.listenerCount(), 0);
});

test('sparse attempts, history rows and files render without the text "undefined"', () => {
  const html = renderAttempt({ conflict_names: [{}, null] }) + renderHistoryRow({}, {}) + renderDiffFiles({ files: [{}] });
  assert.doesNotMatch(html, /undefined|null/);
  assert.match(html, /—/);
});
