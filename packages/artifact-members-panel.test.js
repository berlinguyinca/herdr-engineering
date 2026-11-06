import test from 'node:test';
import assert from 'node:assert/strict';
import {
  renderMemberRow, renderMemberDetail, renderMemberLinks, renderMembersTable, mibToBytes, bytesToMib,
} from './artifact-members-panel.js';

const IMG = '<img src=x onerror=alert(1)>';
const assertInert = html => {
  assert.doesNotMatch(html, /<img/i);
  assert.doesNotMatch(html, /<script/i);
  assert.doesNotMatch(html, /<(iframe|frame|object|embed)/i);
  assert.doesNotMatch(html.replace(/"[^"]*"/g, '""').replace(/>[^<]*</g, '><'), /\son[a-z]+=/i);
};

const MIB = 1048576;
const usage = (extra = {}) => ({
  member_principal: 'member:alice', display_name: 'Alice', bytes_used: 3 * MIB, bytes_retracted: MIB / 2,
  sharing_disabled: false, last_used_at: '2026-11-01T10:00:00+00:00', artifact_count: 4, version_count: 9,
  max_artifact_bytes: null, max_member_bytes: null, max_artifacts: null,
  limits: { max_artifact_bytes: null, max_member_bytes: null, max_artifacts: null },
  live_links: { lab: 1, public: 2 }, ...extra,
});
const detail = (extra = {}) => ({
  member: 'member:alice', usage: usage(), artifacts: [], links: [], ...extra,
});
const link = (extra = {}) => ({
  share_id: 'sh1', artifact_id: 'art1', title: 'Report', audience: 'public', state: 'live', created_by: 'member:alice',
  created_at: '2026-11-01T10:00:00+00:00', expires_at: '2026-12-01T10:00:00+00:00', recipients: [], ...extra,
});

test('a member row shows stored and deleted-not-purged bytes and artifact and version counts', () => {
  const html = renderMemberRow(usage());
  assert.match(html, /Alice/);
  assert.match(html, /3\.0 MiB/);
  assert.match(html, /512\.0 KiB/);
  assert.match(html, /4 artifacts/);
  assert.match(html, /9 versions/);
  assert.match(html, /data-member="member:alice"/);
});

test('null limits render unlimited and no bar', () => {
  const html = renderMemberRow(usage());
  assert.match(html, /unlimited/);
  assert.doesNotMatch(html, /<progress/);
});

test('a set limit renders a bar with the used value and the limit as max', () => {
  const html = renderMemberRow(usage({ limits: { max_artifact_bytes: null, max_member_bytes: 6 * MIB, max_artifacts: 8 } }));
  assert.match(html, /<progress[^>]*value="3145728"[^>]*max="6291456"/);
  assert.match(html, /<progress[^>]*value="4"[^>]*max="8"/);
});

test('a used amount over the limit keeps the bar at the limit', () => {
  const html = renderMemberRow(usage({ limits: { max_artifact_bytes: null, max_member_bytes: MIB, max_artifacts: null } }));
  assert.match(html, /<progress[^>]*value="1048576"[^>]*max="1048576"/);
});

test('a disabled member shows Sharing disabled', () => {
  assert.match(renderMemberRow(usage({ sharing_disabled: true })), /Sharing disabled/);
  assert.doesNotMatch(renderMemberRow(usage()), /Sharing disabled/);
});

test('hostile display names and principals are escaped everywhere', () => {
  const hostile = usage({ display_name: IMG, member_principal: `member:${IMG}` });
  assertInert(renderMemberRow(hostile));
  assertInert(renderMembersTable([hostile]));
  assertInert(renderMemberDetail(detail({
    member: `member:${IMG}`, usage: hostile,
    artifacts: [{ artifact_id: IMG, title: IMG, lifecycle: 'retracted', bytes: 1 }],
    links: [link({ share_id: IMG, artifact_id: IMG, title: IMG, created_by: `member:${IMG}`, recipients: [IMG] })],
  })));
  assertInert(renderMemberLinks([link({ share_id: IMG, artifact_id: IMG, title: IMG, created_by: IMG, expires_at: IMG, created_at: IMG })]));
});

test('the empty state says No member artifacts yet', () => {
  assert.match(renderMembersTable([]), /No member artifacts yet/);
  assert.doesNotMatch(renderMembersTable([]), /<table/);
});

test('the table renders one row per member', () => {
  const html = renderMembersTable([usage(), usage({ member_principal: 'member:bob', display_name: 'Bob' })]);
  assert.equal((html.match(/data-member="/g) || []).length, 2);
});

test('the limits form is in MiB, empty meaning unlimited, with the current override filled in', () => {
  const html = renderMemberDetail(detail({ usage: usage({ max_member_bytes: 5 * MIB, max_artifact_bytes: null, max_artifacts: 3 }) }));
  assert.match(html, /<input[^>]*data-limit="max_member_bytes"[^>]*value="5"/);
  assert.match(html, /<input[^>]*data-limit="max_artifact_bytes"[^>]*placeholder="unlimited"/);
  assert.doesNotMatch(html.match(/<input[^>]*data-limit="max_artifact_bytes"[^>]*>/)[0], /value=/);
  assert.match(html, /<input[^>]*data-limit="max_artifacts"[^>]*value="3"/);
  assert.match(html, /MiB/);
});

test('detail offers Disable sharing, or Enable sharing when disabled', () => {
  assert.match(renderMemberDetail(detail()), /data-members-action="disable-sharing"/);
  const off = renderMemberDetail(detail({ usage: usage({ sharing_disabled: true }) }));
  assert.match(off, /data-members-action="enable-sharing"/);
  assert.match(off, /Sharing disabled/);
});

test('only deleted artifacts get a Purge button, carrying the artifact id', () => {
  const html = renderMemberDetail(detail({ artifacts: [
    { artifact_id: 'a-live', title: 'Live one', lifecycle: 'active', bytes: 10 },
    { artifact_id: 'a-gone', title: 'Gone one', lifecycle: 'retracted', bytes: 10 },
  ] }));
  assert.equal((html.match(/data-members-action="purge"/g) || []).length, 1);
  assert.match(html, /data-members-action="purge"[^>]*data-artifact-id="a-gone"/);
});

test('links list member, artifact, created, expiry and Revoke', () => {
  const html = renderMemberLinks([link()]);
  assert.match(html, /Alice|member:alice/);
  assert.match(html, /Report/);
  assert.match(html, /2026-11-01/);
  assert.match(html, /2026-12-01/);
  assert.match(html, /data-members-action="revoke"[^>]*data-share-id="sh1"[^>]*data-member="member:alice"|data-members-action="revoke"[^>]*data-member="member:alice"[^>]*data-share-id="sh1"/);
});

test('a link without expiry says no expiry; no links says so', () => {
  assert.match(renderMemberLinks([link({ expires_at: null })]), /no expiry/);
  assert.match(renderMemberLinks([]), /No links/);
});

test('MiB conversion round-trips and rejects non-positive input', () => {
  assert.equal(mibToBytes('5'), 5 * MIB);
  assert.equal(mibToBytes('0.5'), MIB / 2);
  assert.equal(mibToBytes(''), null);
  assert.equal(mibToBytes('  '), null);
  assert.throws(() => mibToBytes('0'), /positive/);
  assert.throws(() => mibToBytes('-1'), /positive/);
  assert.throws(() => mibToBytes('abc'), /positive/);
  assert.equal(bytesToMib(5 * MIB), '5');
  assert.equal(bytesToMib(null), '');
});
