import test from 'node:test';
import assert from 'node:assert/strict';
import {
  renderShareForm, renderShareRow, renderSummary, renderEvents, renderLinkDialog,
  buildShareRequest, eventCounts, defaultForm, createShareController,
  renderPendingReview, renderShareList, mountSharePanel,
} from './artifact-share-panel.js';

const IMG = '<img src=x onerror=alert(1)>';
const SCRIPT = '"><script>alert(1)</script>';
const QUOTE = '" autofocus onfocus="x';
const HOSTILE = [IMG, SCRIPT, QUOTE];
const SECRET_URL = 'https://share.example/s/TOKEN-abc123/';

const assertInert = html => {
  assert.doesNotMatch(html, /<img/i);
  assert.doesNotMatch(html, /<script/i);
  assert.doesNotMatch(html, /"><script/);
  assert.doesNotMatch(html, /" autofocus/);
  assert.doesNotMatch(html, /onfocus="/);
};

const versions = [
  { version_id: 'v2', version_number: 2, state: 'ready' },
  { version_id: 'v1', version_number: 1, state: 'ready' },
];
const formState = (form = {}, extra = {}) => ({
  form: { ...defaultForm('v2'), ...form }, versions, headVersionId: 'v2', allowEntry: true, busy: false, pending: null, ...extra,
});

// ------------------------------------------------------------------ renderShareForm

test('the form offers Private / Group (lab) / Public and defaults to Private with no link', () => {
  const html = renderShareForm(formState());
  assert.match(html, />Private</);
  assert.match(html, />Group \(lab\)</);
  assert.match(html, />Public</);
  assert.match(html, /<option value="private" selected>/);
  assert.match(html, /No link is created/);
  assert.match(html, /<button[^>]*data-share-action="generate"[^>]*disabled/);
  assert.match(html, /<button[^>]*data-share-action="review"[^>]*disabled/);
});

test('a pending share can be generated whatever the form audience shows', () => {
  const html = renderShareForm(formState({ audience: 'private' }, { pending: { shareId: 's1', summary: {} } }));
  assert.match(html, /No link is created/);
  assert.match(html, /<button[^>]*data-share-action="review"[^>]*disabled/);
  assert.doesNotMatch(html, /<button[^>]*data-share-action="generate"[^>]*disabled/);
});

test('the public form shows a required expiry (default 30, 1-90) and no lab fields', () => {
  const html = renderShareForm(formState({ audience: 'public' }));
  const expiry = html.match(/<input[^>]*data-share-field="publicDays"[^>]*>/)?.[0];
  assert.ok(expiry, 'public expiry field');
  assert.match(expiry, /value="30"/);
  assert.match(expiry, /min="1"/);
  assert.match(expiry, /max="90"/);
  assert.match(expiry, /required/);
  assert.doesNotMatch(html, /data-share-field="emails"/);
  assert.doesNotMatch(html, /data-share-field="groups"/);
  assert.doesNotMatch(html, /No link is created/);
  assert.doesNotMatch(html, /<button[^>]*data-share-action="review"[^>]*disabled/);
});

test('the lab form shows email and group fields and an optional expiry', () => {
  const html = renderShareForm(formState({ audience: 'lab' }));
  assert.match(html, /<textarea[^>]*data-share-field="emails"/);
  assert.match(html, /<textarea[^>]*data-share-field="groups"/);
  assert.match(html, /comma or new line/i);
  const expiry = html.match(/<input[^>]*data-share-field="labDays"[^>]*>/)?.[0];
  assert.ok(expiry, 'lab expiry field');
  assert.doesNotMatch(expiry, /required/);
  assert.match(expiry, /max="3650"/);
  assert.doesNotMatch(html, /data-share-field="publicDays"/);
});

test('Generate is enabled only for a reviewed share and never while a call is in flight', () => {
  assert.match(renderShareForm(formState({ audience: 'public' })), /<button[^>]*data-share-action="generate"[^>]*disabled/);
  const ready = renderShareForm(formState({ audience: 'public' }, { pending: { shareId: 's1', summary: {} } }));
  assert.doesNotMatch(ready, /<button[^>]*data-share-action="generate"[^>]*disabled/);
  const busy = renderShareForm(formState({ audience: 'public' }, { busy: true, pending: { shareId: 's1', summary: {} } }));
  assert.match(busy, /<button[^>]*data-share-action="generate"[^>]*disabled/);
  assert.match(busy, /<button[^>]*data-share-action="review"[^>]*disabled/);
});

test('the form escapes hostile values in every field and version id', () => {
  for (const bad of HOSTILE) {
    const html = renderShareForm({
      ...formState({ audience: 'lab', emails: bad, groups: bad, title: bad, entry: bad, labDays: bad }),
      versions: [{ version_id: bad, version_number: bad, state: 'ready' }], headVersionId: bad,
    });
    assertInert(html);
    assert.match(html, /&lt;|&quot;/);
  }
});

test('the report form has no entry field', () => {
  assert.match(renderShareForm(formState({ audience: 'public' })), /data-share-field="entry"/);
  assert.doesNotMatch(renderShareForm(formState({ audience: 'public' }, { allowEntry: false })), /data-share-field="entry"/);
});

test('the form defaults to the head version and lists only ready versions', () => {
  const html = renderShareForm({ ...formState(), versions: [...versions, { version_id: 'v3', version_number: 3, state: 'uploading' }] });
  assert.match(html, /<option value="v2" selected>/);
  assert.doesNotMatch(html, /value="v3"/);
});

// ------------------------------------------------------------------ buildShareRequest

test('a public request carries the expiry and no allow-lists', () => {
  assert.deepEqual(buildShareRequest({ ...defaultForm('v2'), audience: 'public', emails: 'a@x.org' }),
    { version_id: 'v2', audience: 'public', expires_in_days: 30 });
});

test('a public expiry outside 1-90 or not an integer is refused before any call', () => {
  for (const days of ['0', '91', '', '2.5', 'abc']) {
    assert.throws(() => buildShareRequest({ ...defaultForm('v2'), audience: 'public', publicDays: days }), /1.*90/);
  }
});

test('a lab request splits emails and groups on commas and new lines', () => {
  assert.deepEqual(buildShareRequest({ ...defaultForm('v2'), audience: 'lab', emails: ' a@x.org,\nb@y.org ,, ', groups: 'lab-a\n\nlab-b', title: ' Report ', entry: ' index.html ' }),
    { version_id: 'v2', audience: 'lab', allow_emails: ['a@x.org', 'b@y.org'], allow_groups: ['lab-a', 'lab-b'], title: 'Report', entry: 'index.html' });
});

test('a lab request needs a recipient and an optional 1-3650 expiry', () => {
  assert.throws(() => buildShareRequest({ ...defaultForm('v2'), audience: 'lab' }), /email or group/);
  assert.equal(buildShareRequest({ ...defaultForm('v2'), audience: 'lab', groups: 'g', labDays: '' }).expires_in_days, undefined);
  assert.equal(buildShareRequest({ ...defaultForm('v2'), audience: 'lab', groups: 'g', labDays: '365' }).expires_in_days, 365);
  assert.throws(() => buildShareRequest({ ...defaultForm('v2'), audience: 'lab', groups: 'g', labDays: '3651' }), /3650/);
});

test('private never produces a request and an entry is dropped where it is not allowed', () => {
  assert.throws(() => buildShareRequest(defaultForm('v2')), /No link is created/);
  assert.equal(buildShareRequest({ ...defaultForm('v2'), audience: 'public', entry: 'a.html' }, { allowEntry: false }).entry, undefined);
  assert.throws(() => buildShareRequest({ ...defaultForm(''), audience: 'public' }), /version/);
});

// ------------------------------------------------------------------ renderSummary

test('the summary lists each file with its size, the total and every warning', () => {
  const html = renderSummary({
    entry: 'index.html', file_count: 3, total_bytes: 3072, audience: 'public', expires_at: '2026-11-29T00:00:00+00:00',
    files: [{ name: 'index.html', size_bytes: 1024 }, { name: 'data.csv', size_bytes: 2048 }], warnings: ['w-one', 'w-two'],
  });
  assert.match(html, /index\.html/);
  assert.match(html, /data\.csv/);
  assert.match(html, /1\.0 KB/);
  assert.match(html, /2\.0 KB/);
  assert.match(html, /3\.0 KB/);
  assert.match(html, /w-one/);
  assert.match(html, /w-two/);
  assert.match(html, /1 more file/);
  assert.match(html, /Public/);
});

test('the summary escapes hostile file names, entry and warnings', () => {
  for (const bad of HOSTILE) {
    const html = renderSummary({ entry: bad, file_count: 1, total_bytes: 1, files: [{ name: bad, size_bytes: null }], warnings: [bad], audience: bad, expires_at: bad });
    assertInert(html);
    assert.match(html, /size unknown/);
  }
});

// ------------------------------------------------------------------ renderShareRow / events

const liveShare = {
  share_id: 'share_1', state: 'live', audience: 'lab', allow_emails: ['a@x.org'], allow_groups: ['lab-a'],
  expires_at: '2026-12-01T00:00:00+00:00', title: 'Q3 report', version_id: 'v2',
};

test('a row shows audience, recipients, state, expiry and opened/denied counts', () => {
  const html = renderShareRow(liveShare, { open: false, events: [{ kind: 'opened' }, { kind: 'opened' }, { kind: 'denied' }, { kind: 'created' }] });
  assert.match(html, /Group \(lab\)/);
  assert.match(html, /a@x\.org/);
  assert.match(html, /lab-a/);
  assert.match(html, /live/);
  assert.match(html, /2026-12-01/);
  assert.match(html, /2 opened/);
  assert.match(html, /1 denied/);
  assert.match(html, /data-share-action="revoke"/);
  assert.match(html, /data-share-action="events"/);
});

test('a row without loaded events shows no invented counts and a public row says Anyone with the link', () => {
  const html = renderShareRow({ ...liveShare, audience: 'public', allow_emails: [], allow_groups: [], expires_at: null });
  assert.match(html, /Public/);
  assert.match(html, /Anyone with the link/);
  assert.doesNotMatch(html, /\d+ opened/);
  assert.match(html, /Never/);
});

test('revoked and expired rows have no Revoke button; pending rows do', () => {
  assert.doesNotMatch(renderShareRow({ ...liveShare, state: 'revoked' }), /data-share-action="revoke"/);
  assert.doesNotMatch(renderShareRow({ ...liveShare, state: 'expired' }), /data-share-action="revoke"/);
  assert.match(renderShareRow({ ...liveShare, state: 'pending' }), /data-share-action="revoke"/);
});

test('a row escapes hostile share fields and an open event drawer escapes event fields', () => {
  for (const bad of HOSTILE) {
    const html = renderShareRow(
      { share_id: bad, state: bad, audience: bad, allow_emails: [bad], allow_groups: [bad], expires_at: bad, title: bad, version_id: bad },
      { open: true, events: [{ kind: bad, actor: bad, at: bad, detail: JSON.stringify({ reason: bad }) }] });
    assertInert(html);
  }
  const failed = renderShareRow(liveShare, { open: true, error: IMG });
  assertInert(failed);
  assert.match(failed, /&lt;img/);
});

test('the event drawer lists opened, denied, created and revoked lines', () => {
  const html = renderEvents([
    { kind: 'created', actor: 'owner@x', at: '2026-10-30T10:00:00' },
    { kind: 'opened', actor: 'a@x.org', at: '2026-10-30T11:00:00' },
    { kind: 'denied', actor: 'z@q.org', at: '2026-10-30T12:00:00', detail: '{"reason": "not allowed"}' },
    { kind: 'revoked', actor: 'owner@x', at: '2026-10-30T13:00:00' },
  ]);
  for (const kind of ['created', 'opened', 'denied', 'revoked']) assert.match(html, new RegExp(kind));
  assert.match(html, /not allowed/);
  assert.deepEqual(eventCounts([{ kind: 'opened' }, { kind: 'denied' }, { kind: 'opened' }]), { opened: 2, denied: 1 });
});

test('the link dialog never carries the URL in its markup', () => {
  const html = renderLinkDialog({ entry: 'index.html', file_count: 1, total_bytes: 10, warnings: [IMG] });
  assert.match(html, /This link cannot be shown again/);
  assert.match(html, /<input[^>]*data-share-url[^>]*readonly/);
  assert.match(html, /data-share-action="copy"/);
  assert.match(html, /data-share-action="close"/);
  assertInert(html);
});

// ------------------------------------------------------------------ controller (fake client)

function deferred() {
  let resolve, reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

function fakeClient(overrides = {}) {
  const calls = [];
  const record = (name, value) => (...args) => { calls.push([name, ...args]); return typeof value === 'function' ? value(...args) : Promise.resolve(value); };
  return {
    calls,
    count: name => calls.filter(call => call[0] === name).length,
    listArtifactShares: record('list', overrides.list ?? { shares: [] }),
    createArtifactShare: record('create', overrides.create ?? { share: { share_id: 'share_new', state: 'pending' }, summary: { files: [], warnings: [] } }),
    approveArtifactShare: record('approve', overrides.approve ?? { share: { share_id: 'share_new', state: 'live' }, url: SECRET_URL, summary: { warnings: [] } }),
    revokeArtifactShare: record('revoke', overrides.revoke ?? { share: { share_id: 'share_1', state: 'revoked' } }),
    artifactShareEvents: record('events', overrides.events ?? { share_id: 'share_1', events: [] }),
  };
}

const tick = () => new Promise(resolve => setImmediate(resolve));
const controller = (client, options = {}) => createShareController(client, 'art1', { versions, headVersionId: 'v2', ...options });

test('the controller loads the list and shows a list error without touching the form', async () => {
  const client = fakeClient({ list: () => Promise.reject(new Error(IMG)) });
  const ctrl = controller(client);
  await ctrl.refresh();
  assert.equal(ctrl.state.shares, null);
  assert.equal(ctrl.state.listError, IMG);
  const ok = controller(fakeClient({ list: { shares: [liveShare] } }));
  await ok.refresh();
  assert.deepEqual(ok.state.shares, [liveShare]);
  assert.equal(ok.state.listError, null);
});

test('private review and generate make no call', async () => {
  const client = fakeClient();
  const ctrl = controller(client);
  await ctrl.review();
  await ctrl.generate();
  assert.equal(client.count('create'), 0);
  assert.equal(client.count('approve'), 0);
});

test('a double-clicked Review creates one share, then the list is refreshed', async () => {
  const gate = deferred();
  const client = fakeClient({ create: () => gate.promise });
  const ctrl = controller(client);
  ctrl.setField('audience', 'public');
  const first = ctrl.review();
  const second = ctrl.review();
  assert.equal(ctrl.state.busy, true);
  gate.resolve({ share: { share_id: 'share_new', state: 'pending' }, summary: { files: [{ name: 'a.html', size_bytes: 1 }], warnings: [] } });
  await Promise.all([first, second]);
  assert.equal(client.count('create'), 1);
  assert.deepEqual(client.calls.find(call => call[0] === 'create').slice(1, 3), ['art1', { version_id: 'v2', audience: 'public', expires_in_days: 30 }]);
  assert.equal(ctrl.state.pending.shareId, 'share_new');
  assert.equal(ctrl.state.busy, false);
  assert.ok(client.count('list') >= 1);
});

test('a double-clicked Generate approves once and the URL lives only in state.link', async () => {
  const gate = deferred();
  const client = fakeClient({ approve: () => gate.promise });
  const ctrl = controller(client);
  ctrl.setField('audience', 'public');
  await ctrl.review();
  const first = ctrl.generate();
  const second = ctrl.generate();
  gate.resolve({ share: { share_id: 'share_new', state: 'live' }, url: SECRET_URL, summary: { warnings: [] } });
  await Promise.all([first, second]);
  assert.equal(client.count('approve'), 1);
  assert.equal(client.calls.find(call => call[0] === 'approve')[2], 'share_new');
  assert.equal(ctrl.state.link.url, SECRET_URL);
  assert.equal(ctrl.state.pending, null, 'Generate is disabled until the next Review');
  await ctrl.generate();
  assert.equal(client.count('approve'), 1, 'a second Generate cannot approve again');
  // No rendered region except the dialog input (set via .value) may carry the URL.
  const rendered = renderShareForm({ ...ctrl.state, versions, headVersionId: 'v2', allowEntry: true })
    + (ctrl.state.shares || []).map(share => renderShareRow(share)).join('') + renderLinkDialog(ctrl.state.link.summary);
  assert.doesNotMatch(rendered, /TOKEN-abc123/);
  ctrl.closeLink();
  assert.equal(ctrl.state.link, null);
  await tick();
  assert.ok(client.count('list') >= 2, 'closing the link refreshes the list');
});

test('a failed Generate shows the escaped server error, no link, and refreshes the list', async () => {
  const error = Object.assign(new Error(SCRIPT), { status: 409 });
  const client = fakeClient({ approve: () => Promise.reject(error) });
  const ctrl = controller(client);
  ctrl.setField('audience', 'public');
  await ctrl.review();
  const before = client.count('list');
  await ctrl.generate();
  assert.equal(ctrl.state.link, null);
  assert.ok(ctrl.state.error.startsWith(SCRIPT));
  assert.match(ctrl.state.error, /revoke it and generate a new one/);
  assert.equal(ctrl.state.pending, null, 'a 409 means the share is no longer pending');
  assert.equal(ctrl.state.busy, false);
  assert.ok(client.count('list') > before);
});

test('editing the form after Review drops the reviewed share, and a new Review clears an open link', async () => {
  const client = fakeClient();
  const ctrl = controller(client);
  ctrl.setField('audience', 'public');
  await ctrl.review();
  ctrl.setField('title', 'changed');
  assert.equal(ctrl.state.pending, null);
  assert.match(ctrl.state.notice, /Review & generate/, 'the set-aside share is announced with its way back');
  await ctrl.generate();
  assert.equal(client.count('approve'), 0);
  await ctrl.review();
  await ctrl.generate();
  assert.equal(ctrl.state.link.url, SECRET_URL);
  const gate = deferred();
  client.createArtifactShare = () => gate.promise;
  const pending = ctrl.review();
  assert.equal(ctrl.state.link, null);
  gate.resolve({ share: { share_id: 'share_b', state: 'pending' }, summary: {} });
  await pending;
});

test('an invalid form shows a validation error and makes no call', async () => {
  const client = fakeClient();
  const ctrl = controller(client);
  ctrl.setField('audience', 'lab');
  await ctrl.review();
  assert.match(ctrl.state.error, /email or group/);
  assert.equal(client.count('create'), 0);
});

test('Revoke asks first, revokes once and refreshes', async () => {
  const client = fakeClient();
  const ctrl = controller(client);
  await ctrl.revoke('share_1', () => false);
  assert.equal(client.count('revoke'), 0);
  const gate = deferred();
  client.revokeArtifactShare = (...args) => { client.calls.push(['revoke', ...args]); return gate.promise; };
  const first = ctrl.revoke('share_1', () => true);
  const second = ctrl.revoke('share_1', () => true);
  gate.resolve({ share: { share_id: 'share_1', state: 'revoked' } });
  await Promise.all([first, second]);
  assert.equal(client.count('revoke'), 1);
  assert.ok(client.count('list') >= 1);
});

test('events load lazily, dedupe in-flight reads and reload when reopened', async () => {
  const client = fakeClient({ events: { share_id: 'share_1', events: [{ kind: 'opened' }, { kind: 'denied' }] } });
  const ctrl = controller(client);
  // A quick open/close/open while the first read is in flight still reads once.
  await Promise.all([ctrl.toggleEvents('share_1'), ctrl.toggleEvents('share_1'), ctrl.toggleEvents('share_1')]);
  assert.equal(client.count('events'), 1);
  const entry = ctrl.state.events.get('share_1');
  assert.equal(entry.open, true);
  assert.equal(entry.events.length, 2);
  await ctrl.toggleEvents('share_1');
  assert.equal(ctrl.state.events.get('share_1').open, false);
  await ctrl.toggleEvents('share_1');
  assert.equal(ctrl.state.events.get('share_1').open, true);
  assert.equal(client.count('events'), 2, 'reopening reloads the events');
  assert.match(renderShareRow(liveShare, ctrl.state.events.get('share_1')), /1 opened · 1 denied/);
});

test('responses after dispose are dropped and the link is cleared', async () => {
  const gate = deferred();
  const client = fakeClient({ approve: () => gate.promise });
  const ctrl = controller(client);
  ctrl.setField('audience', 'public');
  await ctrl.review();
  const pending = ctrl.generate();
  ctrl.dispose();
  gate.resolve({ share: { share_id: 'share_new' }, url: SECRET_URL, summary: {} });
  await pending;
  assert.equal(ctrl.state.link, null);
});

test('an aborted route signal disposes the controller', async () => {
  const route = new AbortController();
  const ctrl = controller(fakeClient(), { signal: route.signal });
  route.abort();
  assert.equal(ctrl.disposed, true);
});

// ------------------------------------------------------------------ fix round 1

const pendingShare = { share_id: 'share_p', state: 'pending', audience: 'lab', allow_emails: ['p@x.org'], allow_groups: [], expires_at: '2026-12-24T00:00:00+00:00', version_id: 'v1' };
const showPending = { share: pendingShare, summary: { file_count: 1, total_bytes: 9, files: [{ name: 'p.html', size_bytes: 9 }], warnings: [] } };
const withShow = (client, show) => { client.getArtifactShare = (...args) => { client.calls.push(['show', ...args]); return typeof show === 'function' ? show(...args) : Promise.resolve(show); }; return client; };

test('a pending row offers Review & generate, disabled (like Revoke) while a call is in flight', () => {
  const html = renderShareRow(pendingShare);
  assert.match(html, /<button[^>]*data-share-action="continue"[^>]*data-share-id="share_p"[^>]*>Review &amp; generate</);
  assert.doesNotMatch(renderShareRow({ ...pendingShare, state: 'live' }), /data-share-action="continue"/);
  const busy = renderShareRow(pendingShare, undefined, { busy: true });
  assert.match(busy, /<button[^>]*data-share-action="continue"[^>]*disabled/);
  assert.match(busy, /<button[^>]*data-share-action="revoke"[^>]*disabled/);
  assert.doesNotMatch(renderShareRow(pendingShare), /data-share-action="revoke"[^>]*disabled/);
});

test('resume loads the pending share, shows its own audience, recipients and expiry, and generate approves it once', async () => {
  const client = withShow(fakeClient(), showPending);
  const ctrl = controller(client);
  await ctrl.resume('share_p');
  assert.deepEqual(client.calls.find(call => call[0] === 'show').slice(1, 3), ['art1', 'share_p']);
  assert.equal(ctrl.state.pending.shareId, 'share_p');
  assert.equal(ctrl.state.form.audience, 'private', 'the form is untouched');
  const review = renderPendingReview(ctrl.state.pending);
  assert.match(review, /Group \(lab\)/);
  assert.match(review, /p@x\.org/);
  assert.match(review, /2026-12-24/);
  assert.match(review, /p\.html/);
  assert.doesNotMatch(renderShareForm({ ...ctrl.state }), /<button[^>]*data-share-action="generate"[^>]*disabled/);
  await Promise.all([ctrl.generate(), ctrl.generate()]);
  assert.equal(client.count('approve'), 1);
  assert.equal(client.calls.find(call => call[0] === 'approve')[2], 'share_p');
  assert.equal(ctrl.state.link.url, SECRET_URL);
  assert.equal(ctrl.state.pending, null);
});

test('resume with a summary_error shows it escaped and keeps Generate disabled', async () => {
  const client = withShow(fakeClient(), { share: pendingShare, summary_error: IMG });
  const ctrl = controller(client);
  await ctrl.resume('share_p');
  assert.equal(ctrl.state.pending, null);
  assert.ok(ctrl.state.error.includes(IMG));
  assert.match(renderShareForm({ ...ctrl.state }), /<button[^>]*data-share-action="generate"[^>]*disabled/);
  const notPending = controller(withShow(fakeClient(), { share: { ...pendingShare, state: 'live' } }));
  await notPending.resume('share_p');
  assert.equal(notPending.state.pending, null);
  assert.match(notPending.state.error, /live/);
});

test('a failed approve keeps the pending share so Retry works, and warns about a live link', async () => {
  let fail = true;
  const client = fakeClient({ approve: () => (fail ? Promise.reject(Object.assign(new Error('gateway timeout'), { status: 504 })) : Promise.resolve({ share: {}, url: SECRET_URL, summary: {} })) });
  const ctrl = controller(client);
  ctrl.setField('audience', 'public');
  await ctrl.review();
  await ctrl.generate();
  assert.equal(ctrl.state.link, null);
  assert.equal(ctrl.state.pending.shareId, 'share_new');
  assert.match(ctrl.state.error, /gateway timeout/);
  assert.match(ctrl.state.error, /If this link now shows as live, revoke it and generate a new one\./);
  fail = false;
  await ctrl.generate();
  assert.equal(client.count('approve'), 2);
  assert.equal(ctrl.state.link.url, SECRET_URL);
});

test('resume after a failed approve lets the retry succeed', async () => {
  let fail = true;
  const client = withShow(fakeClient({ approve: () => (fail ? Promise.reject(new Error('network')) : Promise.resolve({ share: {}, url: SECRET_URL, summary: {} })) }), showPending);
  const ctrl = controller(client);
  await ctrl.resume('share_p');
  await ctrl.generate();
  fail = false;
  await ctrl.resume('share_p');
  await ctrl.generate();
  assert.equal(ctrl.state.link.url, SECRET_URL);
});

test('an approve the server refuses as no longer pending drops the pending share', async () => {
  for (const status of [409, 410]) {
    const client = withShow(fakeClient({ approve: () => Promise.reject(Object.assign(new Error('share is live; a link is issued only once'), { status })) }), showPending);
    const ctrl = controller(client);
    await ctrl.resume('share_p');
    await ctrl.generate();
    assert.equal(ctrl.state.pending, null);
    assert.match(ctrl.state.error, /issued only once/);
  }
});

test('create and approve get the long write deadline; reads keep the short one', async () => {
  const deadlines = [];
  const client = withShow(fakeClient(), showPending);
  const ctrl = controller(client, { timeout: ms => { deadlines.push(ms); return new AbortController().signal; } });
  await ctrl.refresh();
  assert.deepEqual(deadlines, [15000]);
  deadlines.length = 0;
  ctrl.setField('audience', 'public');
  await ctrl.review();
  assert.equal(deadlines[0], 5 * 60 * 1000);
  deadlines.length = 0;
  await ctrl.generate();
  assert.equal(deadlines[0], 5 * 60 * 1000);
  // The default deadline factory never gives create or approve a 15 s timeout.
  const real = fakeClient();
  const signals = [];
  real.approveArtifactShare = (id, sid, { signal }) => { signals.push(signal); return Promise.resolve({ share: {}, url: SECRET_URL, summary: {} }); };
  const live = controller(real);
  live.setField('audience', 'public');
  await live.review();
  await live.generate();
  assert.equal(signals[0].aborted, false);
});

test('after each action the list is re-read and open drawers reload; the first 10 live rows get counts', async () => {
  const shares = Array.from({ length: 12 }, (_, index) => ({ ...liveShare, share_id: `s${index}` }));
  shares.push({ ...liveShare, share_id: 'r1', state: 'revoked' });
  const client = fakeClient({ list: { shares }, events: (id, sid) => (sid === 's1' ? Promise.reject(new Error('quiet')) : Promise.resolve({ events: [{ kind: 'opened' }] })) });
  const ctrl = controller(client);
  await ctrl.refresh();
  const eager = client.calls.filter(call => call[0] === 'events').map(call => call[2]);
  assert.deepEqual(eager.sort(), Array.from({ length: 10 }, (_, index) => `s${index}`).sort());
  assert.match(renderShareRow(shares[0], ctrl.state.events.get('s0')), /1 opened · 0 denied/);
  assert.doesNotMatch(renderShareRow(shares[1], ctrl.state.events.get('s1')), /quiet|opened ·/, 'an eager failure stays silent');
  await ctrl.toggleEvents('r1');
  const before = client.calls.filter(call => call[0] === 'events' && call[2] === 'r1').length;
  await ctrl.revoke('s0', () => true);
  await tick(); await tick();
  assert.ok(client.calls.filter(call => call[0] === 'events' && call[2] === 'r1').length > before, 'an open drawer reloads after an action');
});

test('a 404 or 503 list read is a quiet "not available" note without Retry', async () => {
  for (const status of [404, 503]) {
    const ctrl = controller(fakeClient({ list: () => Promise.reject(Object.assign(new Error(QUOTE), { status })) }));
    await ctrl.refresh();
    assert.equal(ctrl.state.listUnavailable, true);
    const html = renderShareList(ctrl.state);
    assert.match(html, /Sharing is not available for this artifact/);
    assert.doesNotMatch(html, /data-share-action="retry"/);
    assertInert(html);
  }
  const other = controller(fakeClient({ list: () => Promise.reject(Object.assign(new Error(QUOTE), { status: 500 })) }));
  await other.refresh();
  const html = renderShareList(other.state);
  assert.match(html, /data-share-action="retry"/);
  assertInert(html);
});

// A minimal fake DOM: the mount's regions are plain objects holding the HTML strings it writes, so the test can see
// every string the panel ever renders and the one input whose .value carries the URL.
function fakeContainer() {
  const listeners = {};
  const makeNode = () => {
    const node = {
      html: '', inputs: {}, buttons: {},
      get innerHTML() { return node.html; },
      set innerHTML(value) { node.html = String(value); node.inputs = {}; node.buttons = {}; written.push(node.html); },
      replaceChildren() { node.innerHTML = ''; },
      querySelector(selector) {
        if (selector === '[data-share-url]') return node.html.includes('data-share-url') ? (node.inputs.url ||= { value: '', focus() {}, select() {} }) : null;
        if (selector === '[data-share-copy-status]') return node.html.includes('data-share-copy-status') ? (node.inputs.copy ||= { textContent: '' }) : null;
        const action = selector.match(/data-share-action="(\w+)"/)?.[1];
        if (action) return node.html.includes(`data-share-action="${action}"`) ? (node.buttons[action] ||= { disabled: false }) : null;
        return null;
      },
    };
    return node;
  };
  const written = [];
  const regions = {};
  const container = makeNode();
  Object.defineProperty(container, 'innerHTML', {
    get() { return container.html + Object.values(regions).map(region => region.html).join(''); },
    set(value) {
      container.html = String(value); written.push(container.html);
      for (const key of Object.keys(regions)) delete regions[key];
      for (const [, name] of container.html.matchAll(/data-share-([a-z]+)(?:-region)?[=>\s]/g)) regions[name] ||= makeNode();
    },
  });
  container.isConnected = true;
  container.written = written;
  container.regions = regions;
  container.querySelector = selector => {
    const named = selector.match(/^\[data-share-([a-z]+)(?:-region)?\]$/)?.[1];
    if (named && regions[named]) return regions[named];
    for (const region of Object.values(regions)) { const found = region.querySelector(selector); if (found) return found; }
    return null;
  };
  container.replaceChildren = () => { container.innerHTML = ''; };
  container.contains = () => true;
  container.addEventListener = (type, fn) => { (listeners[type] ||= new Set()).add(fn); };
  container.removeEventListener = (type, fn) => { listeners[type]?.delete(fn); };
  container.click = (action, extra = {}) => { for (const fn of listeners.click || []) fn({ target: { closest: () => ({ dataset: { shareAction: action, ...extra }, disabled: false }) } }); };
  container.listenerCount = () => Object.values(listeners).reduce((sum, set) => sum + set.size, 0);
  return container;
}

test('the mount puts the URL only in the dialog input value and drops it on Close and on dispose', async () => {
  const container = fakeContainer();
  const client = fakeClient();
  const panel = mountSharePanel(container, client, 'art1', { versions, headVersionId: 'v2' });
  await tick();
  assert.match(container.innerHTML, /data-share-action="review"/);
  panel.controller.setField('audience', 'public');
  await panel.controller.review();
  assert.match(container.regions.review.html, /Review before generating/);
  await panel.controller.generate();
  await tick();
  assert.equal(container.querySelector('[data-share-url]').value, SECRET_URL);
  assert.match(container.regions.link.html, /This link cannot be shown again/);
  container.click('close');
  await tick();
  assert.equal(container.regions.link.html, '');
  assert.equal(container.querySelector('[data-share-url]'), null);
  // Again, then dispose while the dialog is open.
  await panel.controller.review();
  await panel.controller.generate();
  assert.equal(container.querySelector('[data-share-url]').value, SECRET_URL);
  panel.dispose();
  assert.equal(container.innerHTML, '');
  assert.equal(container.querySelector('[data-share-url]'), null);
  assert.equal(container.listenerCount(), 0);
  assert.ok(container.written.length > 5);
  for (const html of container.written) assert.doesNotMatch(html, /TOKEN-abc123/, 'no rendered string ever carried the URL');
});

test('the mount routes a pending row click to resume and renders the pending share review', async () => {
  const container = fakeContainer();
  const client = withShow(fakeClient({ list: { shares: [pendingShare] } }), showPending);
  const panel = mountSharePanel(container, client, 'art1', { versions, headVersionId: 'v2' });
  await tick(); await tick();
  assert.match(container.regions.list.html, /data-share-action="continue"/);
  container.click('continue', { shareId: 'share_p' });
  await tick(); await tick();
  assert.equal(client.count('show'), 1);
  assert.match(container.regions.review.html, /p@x\.org/);
  panel.dispose();
});

test('setVersions refreshes the version choices and keeps a still-valid selection', async () => {
  const container = fakeContainer();
  const panel = mountSharePanel(container, fakeClient(), 'art1', { versions, headVersionId: 'v2' });
  await tick();
  assert.doesNotMatch(container.regions.form.html, /value="v3"/);
  panel.setVersions([{ version_id: 'v3', version_number: 3, state: 'ready' }, ...versions], 'v3');
  assert.match(container.regions.form.html, /value="v3"/);
  assert.equal(panel.controller.state.headVersionId, 'v3');
  assert.equal(panel.controller.state.form.versionId, 'v2');
  panel.controller.state.form.versionId = 'gone';
  panel.setVersions([{ version_id: 'v3', version_number: 3, state: 'ready' }], 'v3');
  assert.equal(panel.controller.state.form.versionId, 'v3');
  panel.dispose();
});

test('setVersions does not clobber a pending review or a shown link', async () => {
  const container = fakeContainer();
  const panel = mountSharePanel(container, fakeClient(), 'art1', { versions, headVersionId: 'v2' });
  await tick();
  panel.controller.setField('audience', 'public');
  await panel.controller.review();
  await panel.controller.generate();
  await tick();
  assert.equal(container.querySelector('[data-share-url]').value, SECRET_URL);
  panel.setVersions([{ version_id: 'v3', version_number: 3, state: 'ready' }, ...versions], 'v3');
  assert.equal(container.querySelector('[data-share-url]').value, SECRET_URL);
  assert.equal(panel.controller.state.link.url, SECRET_URL);
  await panel.controller.review();
  const pending = panel.controller.state.pending;
  assert.ok(pending);
  panel.setVersions(versions, 'v2');
  assert.equal(panel.controller.state.pending, pending);
  assert.match(container.regions.review.html, /Review before generating/);
  panel.dispose();
});

// ------------------------------------------------------------------ tabs

function withPanes(container) {
  const panes = {};
  const tabs = {};
  const base = container.querySelector;
  const pane = name => (panes[name] ||= (() => {
    const node = { hidden: name !== 'links', html: '', listeners: {}, isConnected: true,
      get innerHTML() { return node.html; }, set innerHTML(v) { node.html = String(v); },
      replaceChildren() { node.html = ''; node.input = null; }, querySelector: sel => (sel === '[data-invite-code]' && node.html.includes('data-invite-code') ? (node.input ||= { value: '' }) : null), contains: () => true,
      addEventListener(type, fn) { (node.listeners[type] ||= new Set()).add(fn); },
      removeEventListener(type, fn) { node.listeners[type]?.delete(fn); } };
    return node;
  })());
  container.panes = panes;
  const add = container.addEventListener;
  container.addEventListener = (type, fn) => { (container.listeners ||= {})[type] ||= new Set(); container.listeners[type].add(fn); add(type, fn); };
  container.querySelector = selector => {
    const paneName = selector.match(/^\[data-panel-pane="(\w+)"\]$/)?.[1];
    if (paneName) return pane(paneName);
    const tabName = selector.match(/^\[data-panel-tab="(\w+)"\]$/)?.[1];
    if (tabName) return (tabs[tabName] ||= { setAttribute(k, v) { this[k] = v; } });
    return base(selector);
  };
  container.tabClick = name => { for (const fn of container.listeners?.click || []) fn({ target: { closest: s => (s === '[data-panel-tab]' ? { dataset: { panelTab: name } } : null) } }); };
  return container;
}

test('the panel offers Links, Collaborators and Activity tabs with the Links content untouched', async () => {
  const container = withPanes(fakeContainer());
  const panel = mountSharePanel(container, fakeClient(), 'art1', { versions, headVersionId: 'v2' });
  await tick();
  for (const label of ['Links', 'Collaborators', 'Activity']) assert.match(container.innerHTML, new RegExp(`data-panel-tab="${label.toLowerCase()}"[^>]*>${label}<`));
  assert.match(container.innerHTML, /data-share-action="review"/);
  assert.doesNotMatch(container.innerHTML, /<(iframe|frame|object|embed)/i);
  panel.dispose();
});

test('a 503 on the Collaborators tab shows the notice and the Links tab keeps working', async () => {
  const container = withPanes(fakeContainer());
  const client = { ...fakeClient(), listArtifactCollaborators: async () => { throw Object.assign(new Error('off'), { status: 503 }); } };
  const collabApi = { agents: async () => ({ agents: [] }), rooms: async () => ({ rooms: [] }), inviteExternal: async () => ({}) };
  const panel = mountSharePanel(container, client, 'art1', { versions, headVersionId: 'v2', collabApi });
  await tick();
  container.tabClick('collaborators');
  await tick(); await tick();
  assert.match(container.panes.collaborators.html, /Collaboration is not enabled/);
  assert.doesNotMatch(container.panes.collaborators.html, /data-collab-field/);
  assert.equal(container.panes.links?.hidden, true);
  container.tabClick('links');
  assert.equal(container.panes.links.hidden, false);
  assert.match(container.innerHTML, /data-share-action="review"/);
  panel.dispose();
});

test('leaving the Collaborators tab drops the one-time invite code and disarms Restrict all', async () => {
  const container = withPanes(fakeContainer());
  const restricted = [];
  const client = { ...fakeClient(), listArtifactCollaborators: async () => ({ writable: true, collaborators: [], pending_invites: [] }),
    grantArtifactInvite: async () => ({}), restrictArtifactCollab: async () => { restricted.push(1); return {}; } };
  const collabApi = { agents: async () => ({ agents: [] }), rooms: async () => ({ rooms: [{ room_id: 'r1', name: 'R' }] }), inviteExternal: async () => ({ invite_id: 'i1', code: 'SECRET-CODE', expires_at: 1900000000 }) };
  const panel = mountSharePanel(container, client, 'art1', { versions, headVersionId: 'v2', collabApi });
  container.tabClick('collaborators');
  await tick(); await tick();
  const pane = container.panes.collaborators;
  const paneClick = data => { for (const fn of pane.listeners.click) fn({ target: { closest: s => { const k = s.match(/^\[data-([a-z-]+)\]$/)[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase()); return k in data ? { dataset: data, disabled: false } : null; } } }); };
  for (const fn of pane.listeners.change) fn({ target: { closest: s => (s === '[data-collab-field]' ? { dataset: { collabField: 'room' }, value: 'r1' } : null) } });
  paneClick({ collabAction: 'invite' });
  await tick(); await tick(); await tick();
  assert.equal(pane.querySelector('[data-invite-code]').value, 'SECRET-CODE');
  paneClick({ collabAction: 'restrict' });
  assert.match(pane.html, /restrict-confirm/);
  for (const away of ['links', 'activity']) {
    container.tabClick('collaborators');
    paneClick({ collabAction: 'invite' }); await tick(); await tick(); await tick();
    assert.ok(pane.querySelector('[data-invite-code]'));
    container.tabClick(away);
    assert.equal(pane.querySelector('[data-invite-code]'), null);
    assert.doesNotMatch(pane.html, /SECRET-CODE/);
  }
  container.tabClick('collaborators');
  assert.doesNotMatch(pane.html, /restrict-confirm/);
  assert.equal(restricted.length, 0);
  panel.dispose();
});
