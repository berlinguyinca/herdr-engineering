import test from 'node:test';
import assert from 'node:assert/strict';
import { renderVersionRow, renderVersionList, createHistoryController, mountHistory } from './artifact-history.js';

const IMG = '<img src=x onerror=alert(1)>';
const v = (n, extra = {}) => ({ version_id: `v${n}`, version_number: n, state: 'ready', created_by: 'alice', created_at: '2026-10-30T10:00:00Z', parent_version_id: n > 1 ? `v${n - 1}` : null, ...extra });

test('head row has a current badge and no Restore button', () => {
  const html = renderVersionRow(v(2), { headVersionId: 'v2', canRestore: true });
  assert.match(html, /data-version-head/);
  assert.match(html, /current/i);
  assert.doesNotMatch(html, /data-history-action="restore"/);
});

test('non-head row has Restore only when canRestore', () => {
  assert.match(renderVersionRow(v(1), { headVersionId: 'v2', canRestore: true }), /<button[^>]*data-history-action="restore"[^>]*data-version-id="v1"[^>]*>Restore this version/);
  assert.doesNotMatch(renderVersionRow(v(1), { headVersionId: 'v2', canRestore: false }), /Restore this version/);
});

test('busy disables the restore button', () => {
  assert.match(renderVersionRow(v(1), { headVersionId: 'v2', canRestore: true, busy: true }), /<button[^>]*data-history-action="restore"[^>]*disabled/);
});

test('parents: parent_version_ids preferred, merge marked at 2+, fallback to parent_version_id', () => {
  const merge = renderVersionRow(v(3, { parent_version_ids: ['v2', 'v1'] }), { headVersionId: 'v3' });
  assert.match(merge, /data-version-merge/);
  assert.match(merge, /v2/); assert.match(merge, /v1/);
  const linear = renderVersionRow(v(2), { headVersionId: 'v2' });
  assert.doesNotMatch(linear, /data-version-merge/);
  assert.match(linear, /Parent[^<]*<code>v1<\/code>/);
  const single = renderVersionRow(v(2, { parent_version_ids: ['vX'] }), { headVersionId: 'v2' });
  assert.match(single, /vX/); assert.doesNotMatch(single, /data-version-merge/);
  assert.doesNotMatch(renderVersionRow(v(1), { headVersionId: 'v2' }), /Parent/);
});

test('author, time and state are shown and hostile strings are escaped', () => {
  const html = renderVersionRow(v(2, { created_by: IMG, state: IMG, version_id: IMG, parent_version_ids: [IMG, '"><script>x</script>'] }), { headVersionId: 'x', canRestore: true });
  assert.doesNotMatch(html, /<img/i); assert.doesNotMatch(html, /<script/i);
  assert.match(renderVersionRow(v(2), { headVersionId: 'v2' }), /alice/);
  assert.match(renderVersionRow(v(2), { headVersionId: 'v2' }), /2026-10-30T10:00:00Z/);
});

test('renderVersionList wraps rows in a ul', () => {
  const html = renderVersionList([v(2), v(1)], { headVersionId: 'v2', canRestore: true });
  assert.match(html, /^<ul/);
  assert.equal((html.match(/data-version-row/g) || []).length, 2);
});

const fakeClient = (over = {}) => {
  const calls = [];
  let head = 'v2', list = [v(2), v(1)];
  const client = {
    calls,
    async getArtifact() { calls.push('getArtifact'); return { current_version_id: head }; },
    async artifactVersions() { calls.push('artifactVersions'); return { versions: list }; },
    async restoreArtifactVersion(id, vid) { calls.push(['restore', id, vid]); head = 'v3'; list = [v(3, { parent_version_id: 'v2' }), ...list]; return {}; },
    ...over,
  };
  return client;
};
const tick = () => new Promise(r => setTimeout(r, 0));

test('restore calls the client, refetches, reports Restored as vN, and reports the new head', async () => {
  const client = fakeClient();
  const seen = [];
  const ctrl = createHistoryController(client, 'a1', { versions: [v(2), v(1)], headVersionId: 'v2', canRestore: true, onVersions: (vs, h) => seen.push([vs.length, h]) });
  await ctrl.restore('v1');
  assert.deepEqual(client.calls.filter(c => Array.isArray(c))[0], ['restore', 'a1', 'v1']);
  assert.equal(ctrl.state.headVersionId, 'v3');
  assert.equal(ctrl.state.versions.length, 3);
  assert.equal(ctrl.state.notice, 'Restored as v3');
  assert.equal(ctrl.state.busy, false);
  assert.deepEqual(seen, [[3, 'v3']]);
});

test('double click makes one restore call', async () => {
  let release; const gate = new Promise(r => { release = r; });
  const client = fakeClient({ async restoreArtifactVersion(...a) { client.calls.push(['restore']); await gate; return {}; } });
  const ctrl = createHistoryController(client, 'a1', { versions: [v(2), v(1)], headVersionId: 'v2', canRestore: true });
  const a = ctrl.restore('v1'), b = ctrl.restore('v1');
  assert.equal(ctrl.state.busy, true);
  release(); await Promise.all([a, b]);
  assert.equal(client.calls.filter(c => Array.isArray(c)).length, 1);
});

test('409 shows head-moved message', async () => {
  const client = fakeClient({ async restoreArtifactVersion() { throw Object.assign(new Error('conflict'), { status: 409 }); } });
  const ctrl = createHistoryController(client, 'a1', { versions: [v(2), v(1)], headVersionId: 'v2', canRestore: true });
  await ctrl.restore('v1');
  assert.equal(ctrl.state.error, 'The head moved — reload and try again');
  assert.equal(ctrl.state.busy, false);
});

test('other errors surface their message', async () => {
  const client = fakeClient({ async restoreArtifactVersion() { throw new Error(IMG); } });
  const ctrl = createHistoryController(client, 'a1', { versions: [v(2), v(1)], headVersionId: 'v2', canRestore: true });
  await ctrl.restore('v1');
  assert.match(ctrl.state.error, /onerror/);
});

test('restore does nothing for the head, for report artifacts, or after dispose', async () => {
  const client = fakeClient();
  const ctrl = createHistoryController(client, 'a1', { versions: [v(2), v(1)], headVersionId: 'v2', canRestore: true });
  await ctrl.restore('v2');
  const noRestore = createHistoryController(client, 'a1', { versions: [v(2), v(1)], headVersionId: 'v2', canRestore: false });
  await noRestore.restore('v1');
  ctrl.dispose(); await ctrl.restore('v1');
  assert.equal(client.calls.length, 0);
});

function fakeContainer() {
  const listeners = {};
  const c = {
    html: '', isConnected: true, written: [],
    get innerHTML() { return c.html; },
    set innerHTML(value) { c.html = String(value); c.written.push(c.html); },
    replaceChildren() { c.innerHTML = ''; },
    contains: () => true,
    addEventListener(type, fn) { (listeners[type] ||= new Set()).add(fn); },
    removeEventListener(type, fn) { listeners[type]?.delete(fn); },
    listenerCount: () => Object.values(listeners).reduce((sum, set) => sum + set.size, 0),
    click(versionId) {
      const disabled = new RegExp(`data-history-action="restore"[^>]*data-version-id="${versionId}"[^>]*disabled`).test(c.html);
      for (const fn of listeners.click || []) fn({ target: { closest: () => ({ dataset: { historyAction: 'restore', versionId }, disabled }) } });
    },
  };
  return c;
}

test('mounted: double click restores once; abort then stale response leaves the DOM alone', async () => {
  const container = fakeContainer();
  let release; const gate = new Promise(r => { release = r; });
  const client = fakeClient({ async restoreArtifactVersion() { client.calls.push(['restore']); await gate; return {}; } });
  const route = new AbortController();
  mountHistory(container, client, 'a1', { versions: [v(2), v(1)], headVersionId: 'v2', canRestore: true, signal: route.signal });
  container.click('v1'); container.click('v1');
  assert.match(container.html, /data-history-action="restore"[^>]*disabled/);
  route.abort();
  const writes = container.written.length;
  release(); await tick(); await tick();
  assert.equal(client.calls.filter(c => Array.isArray(c)).length, 1);
  assert.equal(container.written.length, writes, 'nothing written after abort');
  assert.equal(container.html, '');
  assert.equal(container.listenerCount(), 0);
});

test('mounted: success shows the notice and the new head badge; errors are escaped', async () => {
  const container = fakeContainer();
  const client = fakeClient();
  mountHistory(container, client, 'a1', { versions: [v(2), v(1)], headVersionId: 'v2', canRestore: true });
  container.click('v1');
  await tick(); await tick(); await tick();
  assert.match(container.html, /Restored as v3/);
  assert.match(container.html, /data-version-id="v3"><p><strong>v3<\/strong>[^]*?data-version-head/);
  assert.equal((container.html.match(/data-version-head/g) || []).length, 1);

  const bad = fakeContainer();
  mountHistory(bad, fakeClient({ async restoreArtifactVersion() { throw new Error(IMG); } }), 'a1', { versions: [v(2), v(1)], headVersionId: 'v2', canRestore: true });
  bad.click('v1'); await tick(); await tick();
  assert.doesNotMatch(bad.html, /<img/i);
  assert.match(bad.html, /role="alert"/);
});
