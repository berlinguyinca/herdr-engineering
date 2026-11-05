// "My artifacts" renderers (plan 5, Task 12): pure functions from API data to HTML strings.
// Every value that came from the API or from a file name goes through esc(); nothing here touches the DOM.
import { escapeHtml as esc } from './herdr-web-components/util.js';

const KIB = 1024;
const MIB = KIB * 1024;
const GIB = MIB * 1024;
export const NOT_A_MEMBER = 'Your account is not a member of the lab group';
export const PUBLIC_TEXT = "Anyone with the link can view this version. It shows 'Shared by <you>'.";
/** The notice with `<you>` replaced by the member's name when it is known (the server banner shows the same name). */
export const publicText = (name) => (name ? PUBLIC_TEXT.replace('<you>', String(name)) : PUBLIC_TEXT);
export const DELETE_TEXT = 'Its links stop working. Only the operator can recover or purge it.';
export const SHOWN_ONCE = 'Shown once. Copy it now; if you lose it, generate a new link.';
export const DEFAULT_PUBLIC_DAYS = 30;
export const MAX_PUBLIC_DAYS = 90;

export function humanBytes(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0) return '0 B';
  if (n < KIB) return `${Math.round(n)} B`;
  if (n < MIB) return `${(n / KIB).toFixed(1)} KiB`;
  if (n < GIB) return `${(n / MIB).toFixed(1)} MiB`;
  return `${(n / GIB).toFixed(1)} GiB`;
}

export function formatDate(iso) {
  if (typeof iso !== 'string') return 'no expiry';
  const t = Date.parse(iso);
  return Number.isNaN(t) ? 'no expiry' : new Date(t).toISOString().slice(0, 10);
}

const list = (value) => (Array.isArray(value) ? value.filter((row) => row && typeof row === 'object') : []);
const permissions = (row) => (Array.isArray(row?.permissions) ? row.permissions : []);

/** Share is offered on an own active artifact (unless sharing is off), or where the grant says share/admin. */
export function canShare(row, { shared = false, sharingDisabled = false } = {}) {
  if (sharingDisabled) return false;
  if (!shared) return row?.lifecycle === undefined || row.lifecycle === 'active';
  return permissions(row).some((p) => p === 'share' || p === 'admin');
}

export function audienceBadge(audience) {
  const label = audience === 'public' ? 'Public' : 'Lab';
  return `<span class="badge badge-${audience === 'public' ? 'public' : 'lab'}">${esc(label)}</span>`;
}

const STATE_LABEL = { live: 'Live', pending: 'Waiting for approval', revoked: 'Revoked', expired: 'Expired' };
const stateBadge = (state) => `<span class="badge badge-${esc(String(state).replace(/[^a-z]/g, ''))}">${esc(STATE_LABEL[state] || state || 'Unknown')}</span>`;

export function liveLinks(links, artifactId) {
  return list(links).filter((l) => l.artifact_id === artifactId && l.state === 'live');
}

export function renderArtifactList(artifacts, links = []) {
  const rows = list(artifacts);
  if (!rows.length) return '<p class="empty" data-empty>You have no artifacts yet. Choose New artifact to create one.</p>';
  return `<ul class="rows" data-artifact-list>${rows.map((a) => {
    const gone = a.lifecycle !== undefined && a.lifecycle !== 'active';
    const live = liveLinks(links, a.artifact_id);
    const linkText = gone ? 'links stopped' : `${live.length} live link${live.length === 1 ? '' : 's'}`;
    const head = a.head_version_number ? `v${esc(a.head_version_number)}` : 'no version yet';
    return `<li class="row${gone ? ' row-deleted' : ''}" data-artifact-row="${esc(a.artifact_id)}">
<a class="row-title" href="#/a/${esc(encodeURIComponent(a.artifact_id))}">${esc(a.title || a.artifact_id)}</a>
<span class="row-meta"><span data-head>${head}</span> <span data-size>${esc(humanBytes(a.bytes))}</span> <span data-links>${esc(linkText)}</span>${gone ? ' <span class="badge badge-revoked">Deleted</span>' : ''}</span>
</li>`;
  }).join('')}</ul>`;
}

export function renderSharedList(shared) {
  const rows = list(shared);
  if (!rows.length) return '<p class="empty" data-empty>Nothing has been shared with you yet.</p>';
  return `<ul class="rows" data-shared-list>${rows.map((a) => {
    const perms = permissions(a).map(String);
    return `<li class="row" data-artifact-row="${esc(a.artifact_id)}">
<a class="row-title" href="#/s/${esc(encodeURIComponent(a.artifact_id))}">${esc(a.title || a.artifact_id)}</a>
<span class="row-meta"><span data-head>${a.head_version_number ? `v${esc(a.head_version_number)}` : 'no version yet'}</span> <span data-perms>${esc(perms.join(', ') || 'read')}</span>${canShare(a, { shared: true }) ? ' <span class="badge badge-lab">You can share</span>' : ''}</span>
</li>`;
  }).join('')}</ul>`;
}

const limitText = (value, formatter) => (value === null || value === undefined ? 'unlimited' : formatter(value));

export function renderUsage(usage, { stagedBytes = 0 } = {}) {
  if (!usage) return '<p class="empty" data-empty>Usage is not available yet.</p>';
  const limits = usage.limits || {};
  return `<dl class="usage" data-usage>
<div><dt>Stored</dt><dd data-stored>${esc(humanBytes(usage.bytes_used))}</dd></div>
<div><dt>Deleted, not yet purged</dt><dd data-deleted>${esc(humanBytes(usage.bytes_retracted))}</dd></div>
<div><dt>Staged in open uploads</dt><dd data-staged>${esc(humanBytes(stagedBytes))}</dd></div>
<div><dt>Artifacts</dt><dd data-artifacts>${esc(usage.artifact_count ?? 0)}</dd></div>
<div><dt>Versions</dt><dd data-versions>${esc(usage.version_count ?? 0)}</dd></div>
<div><dt>Limit per artifact</dt><dd data-limit="artifact">${esc(limitText(limits.max_artifact_bytes, humanBytes))}</dd></div>
<div><dt>Limit in total</dt><dd data-limit="member">${esc(limitText(limits.max_member_bytes, humanBytes))}</dd></div>
<div><dt>Limit on artifacts</dt><dd data-limit="count">${esc(limitText(limits.max_artifacts, String))}</dd></div>
</dl>${usage.sharing_disabled ? '<p class="notice" data-sharing-off>Sharing is switched off for your account.</p>' : ''}`;
}

export function renderHistory(history) {
  const versions = list(history?.versions);
  if (!versions.length) return '<p class="empty" data-empty>No versions yet.</p>';
  return `<ol class="versions" data-history>${versions.map((v) => `<li data-version="${esc(v.version_id)}">
<strong>v${esc(v.version_number)}</strong>${v.version_id === history.head_version_id ? ' <span class="badge badge-live">Head</span>' : ''}
<span class="row-meta">${esc(formatDate(v.created_at))}</span>
${v.message ? `<span class="version-message">${esc(v.message)}</span>` : ''}
</li>`).join('')}${history.truncated ? '<li class="row-meta">Older versions are not shown.</li>' : ''}</ol>`;
}

export function renderLinks(links, titles = {}) {
  const rows = list(links);
  if (!rows.length) return '<p class="empty" data-empty>You have not created any links.</p>';
  return `<ul class="rows" data-link-list>${rows.map((l) => {
    const open = l.state === 'live' || l.state === 'pending';
    const recipients = Array.isArray(l.recipients) && l.recipients.length ? ` To: ${esc(l.recipients.join(', '))}.` : '';
    return `<li class="row" data-link-row="${esc(l.share_id)}">
<span class="row-title">${esc(titles[l.artifact_id] || l.artifact_id)}</span>
<span class="row-meta">${audienceBadge(l.audience)} ${stateBadge(l.state)} <span data-expiry>Expires ${esc(formatDate(l.expires_at))}</span>${recipients}</span>
${open ? `<button type="button" class="btn btn-danger" data-action="revoke" data-artifact="${esc(l.artifact_id)}" data-share="${esc(l.share_id)}">Revoke</button>` : ''}
</li>`;
  }).join('')}</ul>`;
}

export function renderShareForm({ versions = [], headId = null, groups = [], name = '' } = {}) {
  const options = list(versions).map((v) => `<option value="${esc(v.version_id)}"${v.version_id === headId ? ' selected' : ''}>v${esc(v.version_number)}${v.version_id === headId ? ' (head)' : ''}</option>`).join('');
  return `<form class="card" data-share-form>
<h3>Share a version</h3>
<label>Version <select name="version" required>${options}</select></label>
<fieldset><legend>Audience</legend>
<label class="inline"><input type="radio" name="audience" value="lab" checked> Lab</label>
<label class="inline"><input type="radio" name="audience" value="public"> Public</label></fieldset>
<div data-audience-lab>
<label>Lab groups (comma separated, optional) <input name="groups" autocomplete="off" placeholder="${esc(list(groups.map((g) => ({ g }))).map((r) => r.g).join(', '))}"></label>
<label>Pool emails (comma separated, optional) <input name="emails" autocomplete="off" inputmode="email"></label>
<label>Expires in days (1 to ${MAX_PUBLIC_DAYS}, optional) <input name="lab_days" type="number" min="1" max="${MAX_PUBLIC_DAYS}" inputmode="numeric"></label>
</div>
<div data-audience-public hidden>
<p class="notice" data-public-text>${esc(publicText(name))}</p>
<label>Expires in days (1 to ${MAX_PUBLIC_DAYS}) <input name="public_days" type="number" min="1" max="${MAX_PUBLIC_DAYS}" value="${DEFAULT_PUBLIC_DAYS}" inputmode="numeric"></label>
</div>
<p class="form-error" role="alert" data-form-error hidden></p>
<button type="submit" class="btn btn-primary">Review share</button>
</form>`;
}

export function renderShareSummary(created, { name = '' } = {}) {
  const s = created?.summary || {};
  const files = list(s.files);
  const warnings = Array.isArray(s.warnings) ? s.warnings : [];
  return `<section class="card" data-share-summary>
<h3>Check before approving</h3>
<p><span data-summary-audience>${audienceBadge(created?.audience || s.audience)}</span> Expires <strong data-summary-expiry>${esc(formatDate(s.expires_at))}</strong>. Entry file: <code>${esc(s.entry || 'default')}</code>.</p>
${(created?.audience || s.audience) === 'public' ? `<p class="notice">${esc(publicText(name))}</p>` : ''}
<p>${esc(s.file_count ?? files.length)} file${(s.file_count ?? files.length) === 1 ? '' : 's'}, ${esc(humanBytes(s.total_bytes))}.</p>
<ul class="files" data-summary-files>${files.map((f) => `<li>${esc(f.name)} <span class="row-meta">${esc(humanBytes(f.size_bytes))}</span></li>`).join('')}</ul>
${s.files_truncated ? '<p class="row-meta">The list is shortened.</p>' : ''}
${warnings.length ? `<ul class="notice">${warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}
<p class="form-error" role="alert" data-form-error hidden></p>
<button type="button" class="btn btn-primary" data-action="approve" data-share="${esc(created?.share_id)}">Approve</button>
<button type="button" class="btn" data-action="cancel-share" data-share="${esc(created?.share_id)}">Cancel</button>
</section>`;
}

export function renderSecretUrl(url) {
  return `<section class="card" data-secret>
<h3>Your link</h3>
<p><input class="url" readonly value="${esc(url)}" data-secret-url aria-label="Share link"></p>
<button type="button" class="btn btn-primary" data-action="copy">Copy</button>
<p class="notice" data-shown-once>${esc(SHOWN_ONCE)}</p>
</section>`;
}

export function renderUploads(rows) {
  const items = list(rows);
  if (!items.length) return '<p class="empty" data-empty>No files chosen.</p>';
  return `<ul class="files" data-uploads>${items.map((u) => {
    const pct = u.total ? Math.min(100, Math.round((u.done / u.total) * 100)) : 0;
    const label = { hashing: 'Checking file', uploading: 'Uploading', done: 'Uploaded', paused: 'Paused', error: 'Failed', queued: 'Waiting', resume: 'Choose this file again to resume' }[u.phase] || u.phase;
    const busy = u.phase === 'hashing' || u.phase === 'uploading';
    return `<li class="upload" data-upload="${esc(u.name)}" data-phase="${esc(u.phase)}">
<span class="row-title">${esc(u.name)}</span> <span class="row-meta">${esc(humanBytes(u.size))}</span>
<span class="upload-status" data-status>${esc(label)}${u.error ? `: ${esc(u.error)}` : ''}</span>
${busy || u.phase === 'paused' ? `<progress max="100" value="${pct}" aria-label="${esc(label)} ${esc(u.name)}"></progress> <span class="row-meta">${pct}%</span>` : ''}
${busy ? `<button type="button" class="btn" data-action="pause" data-name="${esc(u.name)}">Pause</button>` : ''}
${u.phase === 'paused' || u.phase === 'error' ? `<button type="button" class="btn" data-action="resume" data-name="${esc(u.name)}">Resume</button>` : ''}
</li>`;
  }).join('')}</ul>`;
}

export function renderDeleteDialog(title) {
  return `<dialog data-delete-dialog aria-labelledby="delete-title"><form method="dialog">
<h3 id="delete-title">Delete ${esc(`"${title}"`)}?</h3>
<p>${esc(DELETE_TEXT)}</p>
<button type="button" class="btn" data-action="cancel-delete">Cancel</button>
<button type="button" class="btn btn-danger" data-action="confirm-delete">Delete</button>
</form></dialog>`;
}

export function renderNav(route, signedIn) {
  if (!signedIn) return '';
  const tab = (href, label, key) => `<a href="${href}" class="tab"${route === key ? ' aria-current="page"' : ''}>${esc(label)}</a>`;
  return `${tab('#/', 'My artifacts', 'mine')}${tab('#/shared', 'Shared with me', 'shared')}${tab('#/links', 'My links', 'links')}${tab('#/usage', 'Usage', 'usage')}`;
}

export function renderError(message) {
  return `<p class="form-error" role="alert" data-error>${esc(message)}</p>`;
}

export function renderSignIn(message) {
  return `<section class="card" data-signin><h2>Sign in</h2><p>Use your lab account to manage your artifacts.</p>${message ? renderError(message) : ''}
<button type="button" class="btn btn-primary" data-action="signin">Sign in</button></section>`;
}

export function renderNotMember() {
  return `<section class="card" data-not-member><h2>No access</h2>${renderError(NOT_A_MEMBER)}
<button type="button" class="btn" data-action="signout">Sign out</button></section>`;
}

export function renderNewForm() {
  return `<form class="card" data-new-form>
<h2>New artifact</h2>
<label>Title <input name="title" required maxlength="200" autocomplete="off"></label>
<label>First file name <input name="filename" required value="index.html" autocomplete="off"></label>
<label>Kind <select name="profile"><option value="web">Web page</option><option value="document">Document</option></select></label>
<p class="form-error" role="alert" data-form-error hidden></p>
<button type="submit" class="btn btn-primary">Create</button>
</form>`;
}

export function renderArtifactHeader(a, { shared = false } = {}) {
  const head = a.head_version_number ? `v${esc(a.head_version_number)}` : 'no version yet';
  return `<header class="card" data-artifact-header="${esc(a.artifact_id)}">
<h2>${esc(a.title || a.artifact_id)}</h2>
<p class="row-meta"><span data-head>${head}</span>${a.bytes !== undefined ? ` <span data-size>${esc(humanBytes(a.bytes))}</span>` : ''}${shared ? ` <span data-perms>${esc(permissions(a).join(', ') || 'read')}</span>` : ''}</p>
</header>`;
}

export function renderUploadPicker() {
  return `<section class="card" data-upload-section>
<h3>Upload files</h3>
<label>Choose files <input type="file" multiple data-file-input></label>
<div data-uploads-region></div>
<label>Message <input name="message" data-publish-message maxlength="500" placeholder="What changed" autocomplete="off"></label>
<p class="form-error" role="alert" data-form-error hidden></p>
<button type="button" class="btn btn-primary" data-action="publish" disabled>Publish</button>
</section>`;
}

export function renderDeleteButton() {
  return '<button type="button" class="btn btn-danger" data-action="delete">Delete artifact</button>';
}
