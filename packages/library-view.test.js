import test from 'node:test';
import assert from 'node:assert/strict';
import { renderFeedList, renderFeedRow, renderChips, renderFolderSidebar } from './member-artifacts-view.js';
import { SEARCH_DEBOUNCE_MS, chipCounts, describe, renderLibrary, rowHref } from './library-view.js';

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

test('the dashboard draws the same row markup as the member site, apart from the title link', () => {
  const dash = rowsOf(renderLibrary({ items, folders, chip: 'all' }));
  const sitePending = renderFeedRow(items[1], { folders });
  const siteMine = renderFeedRow(items[0], { folders });
  const siteExpired = renderFeedRow(items[2], { folders });
  const byId = (id) => dash.find((li) => li.includes(`data-feed-row="${id}"`));
  assert.equal(withoutTitle(byId('a1')), withoutTitle(siteMine));
  assert.equal(byId('a3'), siteExpired);                                  // no link at all: identical
  // the pending row is hidden from "All" (pending is its own chip) and identical on its chip
  assert.equal(byId('a2'), undefined);
  const pendingChip = rowsOf(renderLibrary({ items, folders, chip: 'pending' }));
  assert.equal(withoutTitle(pendingChip[0]), withoutTitle(sitePending));
  assert.match(pendingChip[0], /data-action="accept"[\s\S]*data-action="discard"/);
});

test('active and pending rows link to the artifact page, expired and revoked rows do not', () => {
  const html = renderLibrary({ items, folders, chip: 'all' });
  assert.match(html, /<a class="row-title row-open" data-action="open" data-artifact="a1" href="#\/artifacts\/a1">/);
  assert.equal(rowHref(items[2]), null);
  assert.equal(rowHref(items[3]), null);
  assert.equal(rowHref(items[1]), '#/artifacts/a2');
  assert.doesNotMatch(rowsOf(html).find((l) => l.includes('"a3"')), /href=/);
  assert.equal(rowHref({ artifact_id: 'x/y z', status: 'active' }), '#/artifacts/x%2Fy%20z');
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

test('a folder view shows its name, filing controls and a missing folder is not available', () => {
  const inFolder = renderLibrary({ items: [items[0]], folders, chip: 'all', folderId: 'fld_1' });
  assert.match(inFolder, /<h2>Reading &lt;list&gt;<\/h2>/);
  assert.match(inFolder, /data-action="unfile"[^>]*data-folder="fld_1"/);
  assert.match(inFolder, /data-action="rename-folder"/);
  assert.match(renderLibrary({ items, folders, folderId: 'fld_other' }), /not available/);
  assert.equal(renderFeedList([]).includes('data-empty'), true);
});

test('the search debounce is 150 ms and a refusal shows the server message', () => {
  assert.equal(SEARCH_DEBOUNCE_MS, 150);
  assert.equal(describe({ message: 'pending_cap', body: { message: 'You have 25 pending artifacts' } }), 'You have 25 pending artifacts');
  assert.equal(describe(new Error('HTTP 500')), 'HTTP 500');
  assert.equal(describe(null), 'That did not work.');
});
