import test from 'node:test';
import assert from 'node:assert/strict';
import { renderFeedList, renderFeedRow, renderChips, renderFolderSidebar } from './member-artifacts-view.js';
import { SEARCH_DEBOUNCE_MS, chipCounts, describe, renderLibrary, renderLibraryItem, rowHref } from './library-view.js';

const folders = [{ folder_id: 'fld_1', name: 'Reading <list>', item_count: 1 }];
const base = { sharer: 'member:alice', shared_at: '2026-11-01T10:00:00+00:00', expires_at: null, share_id: null, artifact_type: 'web', size_bytes: 10 };
const items = [
  { ...base, artifact_id: 'a1', title: 'Mine <b>doc</b>', source: 'mine', status: 'active', folder_ids: ['fld_1'] },
  { ...base, artifact_id: 'a2', title: 'Pushed', source: 'pending', status: 'pending', folder_ids: [] },
  { ...base, artifact_id: 'a3', title: 'Old link', source: 'opened', status: 'expired', folder_ids: [], shared_at: '2026-10-01T10:00:00+00:00' },
  { ...base, artifact_id: 'a4', title: 'Revoked link', source: 'opened', status: 'revoked', folder_ids: [], shared_at: '2026-09-01T10:00:00+00:00' },
];

const rowsOf = (html) => html.match(/<li class="row[\s\S]*?<\/li>/g) || [];
const withoutTitle = (li) => li.replace(/<(?:a|button|span) class="row-title[^>]*>[\s\S]*?<\/(?:a|button|span)>/, '<TITLE>');

test('the dashboard draws the member site rows in read-only form, with no write control anywhere', () => {
  const html = renderLibrary({ items, folders, chip: 'all' });
  const pending = renderLibrary({ items, folders, chip: 'pending' });
  const inFolder = renderLibrary({ items: [items[0]], folders, chip: 'all', folderId: 'fld_1' });
  for (const page of [html, pending, inFolder]) {
    assert.doesNotMatch(page, /data-action="(accept|discard|unfile|rename-folder|delete-folder|file|revoke|approve)"/);
    assert.doesNotMatch(page, /<form|<select|data-folder-form/);
  }
  assert.equal(rowsOf(html).length, 3);
});

test('active rows open the library item page, expired and revoked rows do not', () => {
  const html = renderLibrary({ items, folders, chip: 'all' });
  assert.match(html, /<a class="row-title row-open" data-action="open" data-artifact="a1" href="#\/library\/a1">/);
  assert.equal(rowHref(items[2]), null);
  assert.equal(rowHref(items[3]), null);
  assert.equal(rowHref(items[1]), '#/library/a2');
  assert.doesNotMatch(rowsOf(html).find((l) => l.includes('"a3"')), /href=/);
  assert.equal(rowHref({ artifact_id: 'x/y z', status: 'active' }), '#/library/x%2Fy%20z');
});

test('titles and folder names are escaped, and the sidebar links stay inside the dashboard router', () => {
  const html = renderLibrary({ items, folders, chip: 'all', q: '"><script>' });
  assert.ok(!html.includes('<script>') && !html.includes('<b>doc</b>') && !html.includes('<list>'));
  assert.match(html, /value="&quot;&gt;&lt;script&gt;"/);
  assert.match(html, /href="#\/library\?folder=fld_1"/);
  assert.match(html, /<a class="tab" href="#\/library" aria-current="page">All items<\/a>/);
  assert.equal(renderFolderSidebar(folders).includes('href="#/f/fld_1"'), true);        // the member site is unchanged
});

test('chips count the feed by source and pending shows only when there is some', () => {
  assert.deepEqual(chipCounts(items), { all: 3, assigned: 0, opened: 2, mine: 1, pending: 1 });
  const html = renderLibrary({ items, folders, chip: 'opened' });
  assert.equal(rowsOf(html).length, 2);
  assert.ok(html.includes(renderChips(chipCounts(items), 'opened')));
  assert.ok(!renderLibrary({ items: [items[0]], folders: [], chip: 'all' }).includes('data-chip="pending"'));
});

test('a folder view shows its name and a missing folder is not available', () => {
  const inFolder = renderLibrary({ items: [items[0]], folders, chip: 'all', folderId: 'fld_1' });
  assert.match(inFolder, /<h2>Reading &lt;list&gt;<\/h2>/);
  assert.match(renderLibrary({ items, folders, folderId: 'fld_other' }), /not available/);
  assert.equal(renderFeedList([]).includes('data-empty'), true);
});

test('the item page lists versions, files and shares, escapes them and has only read controls', () => {
  const item = { artifact_id: 'a1', title: 'T <b>x</b>', owner: 'member:alice', artifact_type: 'web', updated_at: '2026-11-01T10:00:00+00:00', current_version_id: 'v2', files: [{ path: 'index.html', media_type: 'text/html', size: 12 }] };
  const versions = [{ version_id: 'v2', version_number: 2, state: 'ready', current: true, created_at: '2026-11-01T10:00:00+00:00', files: item.files }, { version_id: 'v1', version_number: 1, state: 'ready', current: false, created_at: '2026-10-01T10:00:00+00:00', files: [{ path: 'old<i>.html', media_type: 'text/html', size: 1 }] }];
  const shares = [{ audience: 'lab_group', state: 'live', active: true, expires_at: null }];
  const html = renderLibraryItem({ item, versions, shares });
  assert.ok(!html.includes('<b>x</b>'));
  assert.match(html, /href="#\/library\/a1\?version=v1"/);
  assert.match(html, /data-action="open-file" data-path="index.html"/);
  assert.match(html, /lab_group · live \(active\)/);
  assert.doesNotMatch(html, /<form|<select|<iframe|revoke|approve|delete/i);
  assert.match(renderLibraryItem({ item, versions, shares, versionId: 'v1' }), /old&lt;i&gt;\.html/);
  assert.match(renderLibraryItem({}), /not available/);
});

test('member-controlled file paths and share audiences are escaped on the item page', () => {
  const evil = '"><img src=x onerror=alert(1)>.html';
  const item = { artifact_id: 'a1', title: 't', owner: 'member:<i>x</i>', artifact_type: 'web<b>', current_version_id: 'v1', files: [{ path: evil, media_type: 'text/<u>', size: 1 }] };
  const versions = [{ version_id: 'v1', version_number: 1, state: 'ready', current: true, created_at: null, files: item.files }];
  const shares = [{ audience: '<script>alert(2)</script>', state: 's"><svg onload=1>', active: false, expires_at: null }];
  const html = renderLibraryItem({ item, versions, shares });
  for (const raw of ['<img', '<script', '<svg', '<i>x', '<b>', '<u>', 'onerror=alert(1)>']) assert.ok(!html.includes(raw), raw);
  assert.match(html, /data-path="&quot;&gt;&lt;img src=x onerror=alert\(1\)&gt;\.html"/);
  assert.match(html, /&lt;script&gt;alert\(2\)&lt;\/script&gt;/);
});

test('the search debounce is 150 ms and a refusal shows the server message', () => {
  assert.equal(SEARCH_DEBOUNCE_MS, 150);
  assert.equal(describe({ message: 'pending_cap', body: { message: 'You have 25 pending artifacts' } }), 'You have 25 pending artifacts');
  assert.equal(describe(new Error('HTTP 500')), 'HTTP 500');
  assert.equal(describe(null), 'That did not work.');
});
