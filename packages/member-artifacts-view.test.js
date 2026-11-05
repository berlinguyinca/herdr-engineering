import test from 'node:test';
import assert from 'node:assert/strict';
import {
  humanBytes, canShare, renderArtifactList, renderSharedList, renderUsage, renderHistory, renderLinks, renderShareForm,
  renderShareSummary, renderSecretUrl, renderUploads, renderDeleteDialog, renderNotMember, renderNav, renderError,
  renderSignIn, PUBLIC_TEXT, DELETE_TEXT, NOT_A_MEMBER,
} from './member-artifacts-view.js';

const HOSTILE = '"><img src=x onerror=window.__x=1><script>window.__x=2</script>';
const inert = (html) => {
  assert.doesNotMatch(html, /<img/i);
  assert.doesNotMatch(html, /<script/i);
  assert.doesNotMatch(html, /<(iframe|frame|object|embed)/i);
  assert.doesNotMatch(html.replace(/"[^"]*"/g, '""').replace(/>[^<]*</g, '><'), /\son[a-z]+=/i);
};

test('humanBytes', () => {
  assert.equal(humanBytes(0), '0 B');
  assert.equal(humanBytes(1536), '1.5 KiB');
  assert.equal(humanBytes(3 * 1048576), '3.0 MiB');
  assert.equal(humanBytes(-1), '0 B');
});

test('the artifact list shows title, head, size and live links; deleted ones are greyed with no links', () => {
  const html = renderArtifactList(
    [{ artifact_id: 'a1', title: 'Report', lifecycle: 'active', head_version_number: 3, bytes: 2097152 },
     { artifact_id: 'a2', title: 'Old', lifecycle: 'tombstoned', head_version_number: 1, bytes: 10 }],
    [{ artifact_id: 'a1', state: 'live' }, { artifact_id: 'a1', state: 'revoked' }, { artifact_id: 'a2', state: 'live' }]);
  assert.match(html, /Report/);
  assert.match(html, /v3/);
  assert.match(html, /2\.0 MiB/);
  assert.match(html, /1 live link\b/);
  assert.match(html, /row-deleted[\s\S]*Old[\s\S]*links stopped/);
  assert.equal(renderArtifactList([]).includes('data-empty'), true);
});

test('hostile titles, names, messages, recipients and ids are escaped everywhere', () => {
  const art = { artifact_id: HOSTILE, title: HOSTILE, lifecycle: 'active', bytes: 1, head_version_number: 1 };
  const outputs = [
    renderArtifactList([art], [{ artifact_id: HOSTILE, state: 'live' }]),
    renderSharedList([{ artifact_id: HOSTILE, title: HOSTILE, permissions: [HOSTILE, 'share'] }]),
    renderHistory({ head_version_id: 'v', versions: [{ version_id: HOSTILE, version_number: HOSTILE, message: HOSTILE, created_at: HOSTILE }] }),
    renderLinks([{ share_id: HOSTILE, artifact_id: HOSTILE, audience: HOSTILE, state: HOSTILE, recipients: [HOSTILE], expires_at: HOSTILE }], { [HOSTILE]: HOSTILE }),
    renderShareForm({ versions: [{ version_id: HOSTILE, version_number: HOSTILE }], headId: HOSTILE, groups: [HOSTILE] }),
    renderShareSummary({ share_id: HOSTILE, audience: HOSTILE, summary: { entry: HOSTILE, files: [{ name: HOSTILE, size_bytes: 1 }], warnings: [HOSTILE], expires_at: HOSTILE } }),
    renderSecretUrl(HOSTILE),
    renderUploads([{ name: HOSTILE, size: 1, phase: HOSTILE, error: HOSTILE, done: 0, total: 1 }, { name: HOSTILE, size: 1, phase: 'uploading', done: 1, total: 2 }]),
    renderDeleteDialog(HOSTILE),
    renderError(HOSTILE),
    renderUsage({ bytes_used: HOSTILE, artifact_count: HOSTILE, limits: { max_artifacts: HOSTILE } }),
  ];
  for (const html of outputs) {
    inert(html);
    assert.match(html, /&lt;|&quot;|&gt;/);
  }
});

test('usage with null limits shows unlimited; a limit is shown only when set', () => {
  const none = renderUsage({ bytes_used: 5 * 1048576, bytes_retracted: 1048576, artifact_count: 2, version_count: 4, limits: { max_artifact_bytes: null, max_member_bytes: null, max_artifacts: null } }, { stagedBytes: 524288 });
  assert.equal((none.match(/unlimited/g) || []).length, 3);
  assert.match(none, /data-stored>5\.0 MiB/);
  assert.match(none, /data-deleted>1\.0 MiB/);
  assert.match(none, /data-staged>512\.0 KiB/);
  const set = renderUsage({ bytes_used: 0, limits: { max_artifact_bytes: 1048576, max_member_bytes: null, max_artifacts: 5 } });
  assert.match(set, /data-limit="artifact">1\.0 MiB/);
  assert.match(set, /data-limit="count">5</);
  assert.equal((set.match(/unlimited/g) || []).length, 1);
  assert.match(renderUsage({ limits: {}, sharing_disabled: true }), /Sharing is switched off/);
});

test('Share is hidden on a shared-with-me artifact with only read', () => {
  assert.equal(canShare({ permissions: ['read'] }, { shared: true }), false);
  assert.equal(canShare({ permissions: ['read', 'share'] }, { shared: true }), true);
  assert.equal(canShare({ permissions: ['admin'] }, { shared: true }), true);
  assert.equal(canShare({ permissions: [] }, { shared: true }), false);
  assert.equal(canShare({ lifecycle: 'active' }), true);
  assert.equal(canShare({ lifecycle: 'tombstoned' }), false);
  assert.equal(canShare({ lifecycle: 'active' }, { sharingDisabled: true }), false);
  assert.equal(canShare({ permissions: ['share'] }, { shared: true, sharingDisabled: true }), false);
});

test('the share form has Lab and Public, the public text exactly, and a prefilled expiry of at most 90 days', () => {
  const html = renderShareForm({ versions: [{ version_id: 'v1', version_number: 1 }, { version_id: 'v2', version_number: 2 }], headId: 'v2' });
  assert.match(html, /value="lab" checked/);
  assert.match(html, /value="public"/);
  assert.ok(html.includes(PUBLIC_TEXT.replace(/&/g, '&amp;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;')));
  assert.equal(PUBLIC_TEXT, "Anyone with the link can view this version. It shows 'Shared by <you>'.");
  assert.match(html, /name="public_days" type="number" min="1" max="90" value="30"/);
  assert.match(html, /<option value="v2" selected>v2 \(head\)/);
});

test('the summary lists files and needs an explicit Approve; the secret URL shows the shown-once notice', () => {
  const html = renderShareSummary({ share_id: 's1', audience: 'public', summary: { entry: 'index.html', file_count: 2, total_bytes: 20, files: [{ name: 'index.html', size_bytes: 10 }, { name: 'a.css', size_bytes: 10 }], expires_at: '2026-12-01T00:00:00+00:00' } });
  assert.match(html, /Public/);
  assert.match(html, /2026-12-01/);
  assert.match(html, /index\.html[\s\S]*a\.css/);
  assert.match(html, /data-action="approve"[^>]*>Approve</);
  assert.ok(html.includes('Anyone with the link can view this version.'));
  assert.doesNotMatch(renderShareSummary({ share_id: 's', audience: 'lab', summary: {} }), /Anyone with the link/);
  assert.match(renderSecretUrl('https://x/s/abc/'), /Shown once/);
  assert.match(renderSecretUrl('https://x/s/abc/'), /data-action="copy"/);
});

test('My links shows an audience badge, expiry and Revoke only for open links', () => {
  const html = renderLinks([
    { share_id: 's1', artifact_id: 'a', audience: 'public', state: 'live', expires_at: '2026-12-01T00:00:00+00:00' },
    { share_id: 's2', artifact_id: 'a', audience: 'lab', state: 'revoked', expires_at: null, recipients: ['x@y.z'] }], { a: 'Report' });
  assert.match(html, /badge-public">Public/);
  assert.match(html, /badge-lab">Lab/);
  assert.equal((html.match(/data-action="revoke"/g) || []).length, 1);
  assert.match(html, /Expires 2026-12-01/);
  assert.match(html, /Report/);
});

test('uploads show Checking file while hashing, progress while uploading, and Pause/Resume', () => {
  const html = renderUploads([
    { name: 'a.bin', size: 100, phase: 'hashing', done: 50, total: 100 },
    { name: 'b.bin', size: 100, phase: 'uploading', done: 25, total: 100 },
    { name: 'c.bin', size: 100, phase: 'paused', done: 25, total: 100 },
    { name: 'd.bin', size: 100, phase: 'resume', done: 0, total: 100 }]);
  assert.match(html, /Checking file/);
  assert.match(html, /value="50"/);
  assert.match(html, /value="25"/);
  assert.equal((html.match(/data-action="pause"/g) || []).length, 2);
  assert.equal((html.match(/data-action="resume"/g) || []).length, 1);
  assert.match(html, /Choose this file again/);
});

test('delete dialog names the artifact and says what happens; sign-in and no-access views', () => {
  const html = renderDeleteDialog('Quarterly report');
  assert.match(html, /Delete &quot;Quarterly report&quot;\?/);
  assert.ok(html.includes(DELETE_TEXT));
  assert.equal(DELETE_TEXT, 'Its links stop working. Only the operator can recover or purge it.');
  assert.ok(renderNotMember().includes('Your account is not a member of the lab group'));
  assert.equal(NOT_A_MEMBER, 'Your account is not a member of the lab group');
  assert.match(renderSignIn(), /data-action="signin"/);
  assert.match(renderNav('links', true), /aria-current="page">My links/);
  assert.equal(renderNav('links', false), '');
});

test('the public notice names the member when the name is known, escaped, and keeps the exact brief text otherwise', async () => {
  const { publicText, renderShareForm, renderShareSummary } = await import('./member-artifacts-view.js');
  assert.equal(publicText(''), PUBLIC_TEXT);
  assert.equal(publicText('Ada'), "Anyone with the link can view this version. It shows 'Shared by Ada'.");
  const html = renderShareForm({ versions: [], name: '<img src=x onerror=1>' });
  assert.ok(!html.includes('<img'));
  assert.ok(renderShareSummary({ audience: 'public', summary: {} }, { name: 'Ada' }).includes('Shared by Ada'));
});

// ---------------------------------------------------------------------------------------------- the library (Task 10)
import { filterFeed, renderFeedRow, renderFeedList, renderChips, renderFolderSidebar, chooseEntry } from './member-artifacts-view.js';

const item = (o = {}) => ({
  artifact_id: 'art-1', title: 'Plate map', sharer: 'ada@lab.example', shared_at: '2026-10-01T10:00:00Z', source: 'mine',
  status: 'active', expires_at: null, share_id: null, folder_ids: [], artifact_type: 'html', size_bytes: 10,
  files: [{ path: 'index.html', sha256: 'a'.repeat(64), size: 10, content_type: 'text/html' }], ...o,
});
const FEED = [
  item(),
  item({ artifact_id: 'art-2', title: 'QC report', sharer: 'Bo', source: 'assigned', shared_at: '2026-10-05T00:00:00Z', folder_ids: ['f1'], artifact_type: 'pdf' }),
  item({ artifact_id: 'art-3', title: 'Old link', sharer: 'Cy', source: 'opened', status: 'expired', shared_at: '2026-09-01T00:00:00Z', expires_at: '2026-09-30T00:00:00Z' }),
  item({ artifact_id: 'art-4', title: 'Agent push', sharer: 'agent', source: 'pending', status: 'pending', shared_at: '2026-10-07T00:00:00Z' }),
];
const ids = (rows) => rows.map((r) => r.artifact_id);

test('filterFeed: default sources hide pending; sources, folder, type, dates and text combine; newest first', () => {
  assert.deepEqual(ids(filterFeed(FEED, {})), ['art-2', 'art-1', 'art-3']);
  assert.deepEqual(ids(filterFeed(FEED, { sources: ['pending'] })), ['art-4']);
  assert.deepEqual(ids(filterFeed(FEED, { sources: ['assigned', 'opened'] })), ['art-2', 'art-3']);
  assert.deepEqual(ids(filterFeed(FEED, { folderId: 'f1' })), ['art-2']);
  assert.deepEqual(ids(filterFeed(FEED, { type: 'PDF' })), ['art-2']);
  assert.deepEqual(ids(filterFeed(FEED, { since: '2026-10-01T10:00:00Z' })), ['art-2', 'art-1']);   // since is inclusive
  assert.deepEqual(ids(filterFeed(FEED, { until: '2026-10-05T00:00:00Z' })), ['art-1', 'art-3']);   // until is exclusive
  assert.deepEqual(ids(filterFeed(FEED, { q: 'QC' })), ['art-2']);
  assert.deepEqual(ids(filterFeed(FEED, { q: 'BO' })), ['art-2']);                                   // sharer, case-insensitive
  assert.deepEqual(ids(filterFeed(FEED, { q: 'pdf' })), ['art-2']);                                  // type
  assert.deepEqual(ids(filterFeed(FEED, { q: 'qc', sources: ['mine'] })), []);
  assert.deepEqual(ids(filterFeed(FEED, { q: 'topic', folders: [{ folder_id: 'f1', name: 'Topic A' }] })), ['art-2']);   // own folder names
  assert.deepEqual(ids(filterFeed(FEED, { q: '  ' })), ['art-2', 'art-1', 'art-3']);
  assert.deepEqual(ids(filterFeed(null, {})), []);
});

test('filterFeed: file names are searched only on readable rows (expired rows reveal nothing but title and sharer)', () => {
  const rows = [item({ files: [{ path: 'secret.csv' }] }), item({ artifact_id: 'x', status: 'revoked', files: [{ path: 'secret.csv' }] })];
  assert.deepEqual(ids(filterFeed(rows, { q: 'secret' })), ['art-1']);
});

test('renderFeedRow: badge per source, escaped text, content link on the content route (never a share token)', () => {
  const badge = (source) => renderFeedRow(item({ source }));
  assert.match(badge('mine'), /badge-mine">Mine</);
  assert.match(badge('assigned'), /badge-assigned">Assigned</);
  assert.match(badge('opened'), /badge-opened">Opened via link</);
  assert.match(badge('pending'), /badge-pending">Pending</);
  const html = renderFeedRow(item({ title: HOSTILE, sharer: HOSTILE, share_id: 'shr_secret', source: 'opened', expires_at: '2026-12-01T00:00:00Z' }));
  inert(html);
  assert.match(html, /href="\/api\/member\/artifacts\/art-1\/content\/index\.html"/);
  assert.doesNotMatch(html, /shr_secret/);
  assert.match(html, /Expires 2026-12-01/);
  assert.match(html, /2026-10-01/);
});

test('renderFeedRow: expired and revoked rows are muted, tagged, and have no href and no button', () => {
  for (const [status, tag] of [['expired', 'Expired'], ['revoked', 'Revoked']]) {
    const html = renderFeedRow(item({ status, source: 'opened', title: 'Gone', sharer: 'Cy' }));
    assert.match(html, /class="row[^"]*row-muted/);
    assert.ok(html.includes(`>${tag}<`));
    assert.match(html, /Gone/);
    assert.match(html, /Cy/);
    assert.doesNotMatch(html, /href=/);
    assert.doesNotMatch(html, /<button/);
    assert.doesNotMatch(html, /<select/);
  }
});

test('renderFeedRow: Accept and Discard only on pending rows; folder select only where folders exist', () => {
  const pending = renderFeedRow(item({ source: 'pending', status: 'pending' }));
  assert.match(pending, /data-action="accept"/);
  assert.match(pending, /data-action="discard"/);
  const mine = renderFeedRow(item());
  assert.doesNotMatch(mine, /data-action="accept"|data-action="discard"/);
  assert.doesNotMatch(mine, /<select/);
  const filed = renderFeedRow(item({ folder_ids: ['f1'] }), { folders: [{ folder_id: 'f1', name: '<b>x</b>' }, { folder_id: 'f2', name: 'B' }], activeFolder: 'f1' });
  assert.match(filed, /<select[^>]*data-action="file"/);
  assert.match(filed, /<option value="f2">B</);
  assert.ok(!filed.includes('<b>'));
  assert.match(filed, /data-action="unfile"/);
  assert.doesNotMatch(renderFeedRow(item(), { folders: [{ folder_id: 'f1', name: 'A' }] }), /data-action="unfile"/);
});

test('chooseEntry prefers index.html, then the first file; none gives null', () => {
  assert.equal(chooseEntry(item({ files: [{ path: 'a.csv' }, { path: 'index.html' }] })).path, 'index.html');
  assert.equal(chooseEntry(item({ files: [{ path: 'a.csv' }] })).path, 'a.csv');
  assert.equal(chooseEntry(item({ files: [] })), null);
  assert.equal(chooseEntry(item({ files: undefined })), null);
});

test('renderFeedList: empty state per filter, otherwise one list', () => {
  assert.match(renderFeedList([]), /Nothing here yet/);
  assert.match(renderFeedList([], { filter: 'pending' }), /Nothing here yet/);
  const html = renderFeedList([item()]);
  assert.match(html, /data-feed/);
  assert.doesNotMatch(html, /Nothing here yet/);
});

test('renderChips: four chips, Pending only when pending > 0, active is pressed, counts shown', () => {
  const none = renderChips({ all: 3, assigned: 1, opened: 1, mine: 1, pending: 0 }, 'all');
  for (const label of ['All', 'Assigned to me', 'Opened via link', 'Mine']) assert.ok(none.includes(label));
  assert.doesNotMatch(none, /Pending/);
  assert.match(none, /data-chip="all"[^>]*aria-pressed="true"/);
  assert.match(none, /data-chip="mine"[^>]*aria-pressed="false"/);
  const some = renderChips({ pending: 2 }, 'pending');
  assert.match(some, /data-chip="pending"[^>]*aria-pressed="true"[^>]*>Pending <span[^>]*>2</);
  assert.match(renderChips({}, 'nonsense'), /data-chip="all"[^>]*aria-pressed="true"/);
});

test('renderFolderSidebar: folders link to #/f/<id>, names escaped, active marked, create form and manage buttons', () => {
  const html = renderFolderSidebar([{ folder_id: 'f1', name: HOSTILE, item_count: 2 }, { folder_id: 'f2', name: 'B', item_count: 0 }], 'f2');
  inert(html);
  assert.match(html, /href="#\/f\/f1"/);
  assert.match(html, /href="#\/f\/f2"[^>]*aria-current="page"/);
  assert.match(html, /data-folder-form/);
  assert.match(html, /data-action="rename-folder"/);
  assert.match(html, /data-action="delete-folder"/);
  assert.doesNotMatch(renderFolderSidebar([], null), /rename-folder/);
  assert.match(renderFolderSidebar([], null), /data-folder-form/);
});

test('renderNav keeps My artifacts reachable at #/mine next to the Library tab', () => {
  const html = renderNav('library', true);
  assert.match(html, /href="#\/"[^>]*aria-current="page">Library/);
  assert.match(html, /href="#\/mine"[^>]*>My artifacts/);
});
