// "My artifacts" page (plan 5, Task 12): sign-in, hash routing and the views. Markup comes only from the escaping
// renderers in member-artifacts-view.js; messages go in with textContent. Tokens stay in memory; the member session
// lives in memory + sessionStorage (member-api.js), never localStorage.
import { createMemberApi, createSessionStore, ApiError } from './member-api.js';
import { sessionStore, validConfig, beginPkce, exchangeCode } from './lab-signin.js';
import { uploadFile, MAX_CHUNKS_PER_RUN } from './member-upload.js';
import * as V from './member-artifacts-view.js';

const PENDING_KEY = 'herdr-member-pkce';
const UPLOADS_KEY = 'herdr-member-uploads';
const MAX_PUBLIC_DAYS = V.MAX_PUBLIC_DAYS;

const $ = (sel, root = document) => root.querySelector(sel);
const app = $('#app');
const nav = $('#nav');
const status = $('#status');
const dialogs = $('#dialogs');

const sessions = createSessionStore();
let cfg = null;
let tokens = null;                 // {access, id}: memory only
let data = { usage: null, artifacts: [], shared_with_me: [], links: [] };
let shownShare = null;             // id of the share whose one-time URL is on screen (the URL itself is only in the DOM)
let approving = false;             // an approve call is in flight: Approve and Cancel are off
let uploads = new Map();           // artifact id -> Map(name -> row)
let files = new Map();             // artifact id -> Map(name -> File)
let draft = null;                  // a share awaiting approval {artifactId, created, key}
let current = { route: 'library', id: null };
let lib = { items: [], folders: [], chip: 'all', q: '' };      // the library feed; filtering is client-side over the fetched list
let searchTimer = null;
const SEARCH_DEBOUNCE_MS = 150;
let busyDelete = null;

function say(message, kind = 'info') {
  status.textContent = message || '';
  status.dataset.kind = kind;
}

function showError(root, message) {
  const el = $('[data-form-error]', root || app);
  if (el) { el.textContent = message; el.hidden = false; } else say(message, 'error');
}
const clearError = (root) => { const el = $('[data-form-error]', root || app); if (el) el.hidden = true; };

/** The name the public banner will show: the display name from /me if the server sends one, else the user id. */
function memberName() {
  const me = data.me || {};
  const named = me.display_name || me.user_id;
  if (typeof named === 'string' && named) return named;
  try {
    const payload = JSON.parse(atob((tokens?.id || '').split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
    const user = payload['cognito:username'] || payload.preferred_username || '';
    return typeof user === 'string' ? user : '';
  } catch { return ''; }
}

function describe(error) {
  if (error instanceof ApiError) {
    if (error.status === 429) return 'Too many requests. Wait a moment and try again.';
    return error.message || `The request failed (${error.status}).`;
  }
  return 'The request failed. Check your connection and try again.';
}

// ------------------------------------------------------------------------------------------------ sign-in
async function exchangeSession(t) {
  const r = await fetch('/api/member/session', {
    method: 'POST', cache: 'no-store', credentials: 'omit',
    headers: { Authorization: `Bearer ${t.access}`, 'X-Id-Token': t.id },
  });
  if (!r.ok) throw new ApiError(r.status, 'session', 'session');
  const got = await r.json();
  if (!got || typeof got.session !== 'string' || typeof got.expires_at !== 'number') throw new ApiError(502, 'session', 'session');
  sessions.set(got);
}

const api = createMemberApi({
  sessions,
  refresh: async () => { if (!tokens) throw new Error('no tokens'); await exchangeSession(tokens); },
  onSignedOut: () => signedOut('Your session ended. Sign in again.'),
});

function signedOut(message) {
  sessions.clear();
  nav.replaceChildren();
  $('#signout').hidden = true;
  app.innerHTML = V.renderSignIn(message);
}

async function startSignIn() {
  const store = sessionStore();
  const p = await beginPkce(cfg);
  try { store?.setItem(PENDING_KEY, JSON.stringify({ state: p.state, verifier: p.verifier })); } catch { /* handled by the callback */ }
  location.assign(p.url);
}

async function handleCallback() {
  const params = new URLSearchParams(location.search);
  history.replaceState(null, '', '/');       // the code must not stay in the address bar
  const store = sessionStore();
  let pending = null;
  try {
    pending = JSON.parse(store?.getItem(PENDING_KEY) || 'null');
    store?.removeItem(PENDING_KEY);
  } catch { pending = null; }
  const code = params.get('code');
  if (params.get('error') || !pending || params.get('state') !== pending.state || !code) {
    return signedOut('Sign-in did not complete. Try again.');
  }
  try {
    tokens = await exchangeCode(cfg, code, pending.verifier);
  } catch {
    return signedOut('Sign-in did not complete. Try again.');
  }
  try {
    await exchangeSession(tokens);
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) {       // the server answers every refusal the same way: not a member
      tokens = null;
      app.innerHTML = V.renderNotMember();
      return undefined;
    }
    return signedOut('Sign-in did not complete. Try again.');
  }
  return enter();
}

function signOut() {
  sessions.clear();
  tokens = null;
  try { sessionStore()?.removeItem(UPLOADS_KEY); } catch { /* nothing */ }
  const q = new URLSearchParams({ client_id: cfg.client_id, logout_uri: cfg.logout_uri || `${location.origin}/` });
  location.assign(`${cfg.cognito_domain}/logout?${q.toString()}`);
}

// ------------------------------------------------------------------------------------------------ data
async function reload() {
  const [me, links] = await Promise.all([api.me(), api.links()]);
  data = {
    me: { display_name: me.display_name, user_id: me.user_id },
    usage: me.usage || null,
    artifacts: Array.isArray(me.artifacts) ? me.artifacts : [],
    shared_with_me: Array.isArray(me.shared_with_me) ? me.shared_with_me : [],
    links: Array.isArray(links.links) ? links.links : [],
  };
}

const titles = () => Object.fromEntries([...data.artifacts, ...data.shared_with_me].map((a) => [a.artifact_id, a.title]));
const findOwn = (id) => data.artifacts.find((a) => a.artifact_id === id);
const findShared = (id) => data.shared_with_me.find((a) => a.artifact_id === id);

// open uploads of this browser (names and sizes only), so a reload can offer to resume them
function readRecord() {
  try { return JSON.parse(sessionStore()?.getItem(UPLOADS_KEY) || '{}') || {}; } catch { return {}; }
}
function writeRecord(rec) {
  try { sessionStore()?.setItem(UPLOADS_KEY, JSON.stringify(rec)); } catch { /* memory only */ }
}
function record(artifactId, name, size) {
  const rec = readRecord();
  const rows = (rec[artifactId] || []).filter((r) => r.name !== name);
  rows.push({ name, size });
  rec[artifactId] = rows;
  writeRecord(rec);
}
function forgetRecord(artifactId) {
  const rec = readRecord();
  delete rec[artifactId];
  writeRecord(rec);
}
const stagedBytes = () => Object.values(readRecord()).flat().reduce((n, r) => n + (Number(r.size) || 0), 0);

// ------------------------------------------------------------------------------------------------ views
function paintNav() {
  nav.innerHTML = V.renderNav(current.route === 'folder' ? 'library' : current.route, true);
  $('#signout').hidden = false;
  const who = $('#who');
  if (who) { who.textContent = memberName(); who.hidden = !who.textContent; }
}

function paint(html) {
  app.innerHTML = html;
}

// ------------------------------------------------------------------------------------------------ library
async function reloadLibrary() {
  const [me, got] = await Promise.all([api.me(), api.library()]);
  data.me = { display_name: me.display_name, user_id: me.user_id };
  lib.items = Array.isArray(got.items) ? got.items : [];
  lib.folders = Array.isArray(got.folders) ? got.folders : [];
}

const CHIP_SOURCES = { all: undefined, assigned: ['assigned'], opened: ['opened'], mine: ['mine'], pending: ['pending'] };

function paintFeed() {
  if (!$('[data-chips-region]') || !$('[data-feed-region]')) return;      // the member left the library while a search was pending
  const folderId = current.route === 'folder' ? current.id : null;
  const scope = V.filterFeed(lib.items, { folderId, sources: ['mine', 'assigned', 'opened', 'pending'] });
  const count = (src) => scope.filter((r) => r.source === src).length;
  const counts = { all: count('mine') + count('assigned') + count('opened'), assigned: count('assigned'), opened: count('opened'), mine: count('mine'), pending: count('pending') };
  if (!counts.pending && lib.chip === 'pending') lib.chip = 'all';
  const rows = V.filterFeed(lib.items, { q: lib.q, sources: CHIP_SOURCES[lib.chip], folderId, folders: lib.folders });
  $('[data-chips-region]').innerHTML = V.renderChips(counts, lib.chip);
  $('[data-feed-region]').innerHTML = V.renderFeedList(rows, { folders: lib.folders, activeFolder: folderId });
}

function viewLibrary() {
  const folder = current.route === 'folder' ? lib.folders.find((f) => f.folder_id === current.id) : null;
  if (current.route === 'folder' && !folder) { paint('<p class="empty" data-empty>This folder is not available.</p>'); return; }
  paint(`<div class="library">${V.renderFolderSidebar(lib.folders, folder ? folder.folder_id : null)}
<section class="library-main"><h2>${folder ? V.esc(folder.name) : 'Library'}</h2>
<label class="search">Search <input type="search" data-search maxlength="200" autocomplete="off" placeholder="Title, sharer, folder" value="${V.esc(lib.q)}"></label>
<div data-chips-region></div><div data-feed-region></div></section></div>`);
  paintFeed();
}

/** Run one library write, refresh the view, then say what happened. A refusal (HTTP 200 result "rejected") or a failure is
 *  an error message, never the success text; the view is refreshed either way so it shows the server's truth. */
async function wait(work, done) {
  let message = done;
  let kind = 'info';
  let ok = true;
  try {
    const refusal = V.rejectionText(await work());
    if (refusal) { message = refusal; kind = 'error'; ok = false; }
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) throw e;
    message = describe(e); kind = 'error'; ok = false;
  }
  await route();
  if (message) say(message, kind);
  return ok;
}

async function folderCreate(form) {
  const name = String(new FormData(form).get('name') || '').trim();
  if (!name) return;
  await wait(() => api.createFolder(name), 'Folder created.');
}
async function folderRename(el) {
  const folder = lib.folders.find((f) => f.folder_id === el.dataset.folder);
  const name = (window.prompt('Folder name', folder?.name || '') || '').trim();
  if (name && folder && name !== folder.name) await wait(() => api.renameFolder(folder.folder_id, name), 'Folder renamed.');
}
async function folderDelete(el) {
  const folder = lib.folders.find((f) => f.folder_id === el.dataset.folder);
  if (!folder || !window.confirm(`Delete the folder "${folder.name}"? Its items stay in your library.`)) return;
  const wasHere = current.route === 'folder' && current.id === folder.folder_id;
  if (!wasHere) { await wait(() => api.deleteFolder(folder.folder_id), 'Folder deleted.'); return; }
  let refusal = null;
  try { refusal = V.rejectionText(await api.deleteFolder(folder.folder_id)); } catch (e) { refusal = describe(e); }
  if (refusal) { await route(); say(refusal, 'error'); return; }
  location.hash = '#/';
  say('Folder deleted.');
}

function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = name; a.hidden = true;
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

/** Fetch with the member session. Only the server's inline types (plain text, pdf, png, jpeg, gif, webp) are shown, in their own
 *  window; HTML and every other type is saved as a file and never rendered in the member origin. */
async function openContent(el) {
  const { artifact, path } = el.dataset;
  const { blob, type } = await api.fetchContent(artifact, path);
  const name = path.split('/').pop();
  if (V.contentMode(type) !== 'inline') { saveBlob(blob, name); return; }
  const shown = type.startsWith('text/') ? new Blob([blob], { type: 'text/plain;charset=utf-8' }) : blob;
  const url = URL.createObjectURL(shown);
  const win = window.open(url, '_blank', 'noopener');
  if (win === null) { URL.revokeObjectURL(url); saveBlob(blob, name); say('Your browser blocked the new window, so the file was saved instead.'); return; }
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

function viewMine() {
  paint(`<div class="toolbar"><h2>My artifacts</h2><a class="btn btn-primary" href="#/new">New artifact</a></div>${V.renderArtifactList(data.artifacts, data.links)}`);
}

function viewShared() {
  paint(`<h2>Shared with me</h2>${V.renderSharedList(data.shared_with_me)}`);
}

function viewUsage() {
  paint(`<h2>Usage</h2>${V.renderUsage(data.usage, { stagedBytes: stagedBytes() })}`);
}

function viewLinks() {
  paint(`<h2>My links</h2>${V.renderLinks(data.links, titles())}`);
}

function viewNew() {
  paint(V.renderNewForm());
}

function rowsFor(id) {
  const live = uploads.get(id) || new Map();
  const out = [...live.values()];
  for (const r of readRecord()[id] || []) {
    if (!live.has(r.name)) out.push({ name: r.name, size: r.size, phase: 'resume', done: 0, total: 0 });
  }
  return out;
}

function paintUploads(id) {
  const region = $('[data-uploads-region]');
  if (!region || current.id !== id) return;
  const rows = rowsFor(id);
  region.innerHTML = V.renderUploads(rows);
  const publish = $('[data-action="publish"]');
  if (publish) publish.disabled = !(rows.length && rows.every((r) => r.phase === 'done'));
}

async function viewArtifact(id) {
  const own = findOwn(id);
  const shared = own ? null : findShared(id);
  const row = own || shared;
  if (!row) { paint('<p class="empty" data-empty>This artifact is not available.</p>'); return; }
  const history = await api.history(id).catch(() => ({ versions: [] }));
  if (current.id !== id) return;
  const links = data.links.filter((l) => l.artifact_id === id);
  const sharable = V.canShare(row, { shared: !own, sharingDisabled: !!data.usage?.sharing_disabled });
  const versions = history.versions || [];
  paint([
    V.renderArtifactHeader(row, { shared: !own }),
    own ? V.renderUploadPicker() : '',
    '<section class="card"><h3>Version history</h3>', V.renderHistory(history), '</section>',
    sharable ? V.renderShareForm({ versions, headId: history.head_version_id, name: memberName() }) : '',
    '<div data-share-region></div>',
    '<section class="card"><h3>Links</h3>', V.renderLinks(links, titles()), '</section>',
    own ? V.renderDeleteButton() : '',
  ].join(''));
  if (own) paintUploads(id);
}

async function route() {
  clearTimeout(searchTimer);
  shownShare = null;
  const abandoned = draft;
  draft = null;
  // A share still waiting for approval is revoked when the member leaves it (Cancel does the same): no stray pending link.
  if (abandoned && !approving) api.shareRevoke(abandoned.artifactId, abandoned.created.share_id).catch(() => {});
  say('');
  const hash = location.hash || '#/';
  const m = /^#\/(a|s)\/(.+)$/.exec(hash);
  if (m) {
    let id = null;
    try { id = decodeURIComponent(m[2]); } catch { /* a malformed escape: shown as not found below */ }
    current = { route: 'artifact', id };
  }
  else if (hash === '#/shared') current = { route: 'shared', id: null };
  else if (hash === '#/links') current = { route: 'links', id: null };
  else if (hash === '#/usage') current = { route: 'usage', id: null };
  else if (hash === '#/new') current = { route: 'new', id: null };
  else if (hash === '#/mine') current = { route: 'mine', id: null };
  else {
    const f = /^#\/f\/(.+)$/.exec(hash);
    let fid = null;
    try { fid = f ? decodeURIComponent(f[1]) : null; } catch { /* malformed: the folder is not found below */ }
    current = f ? { route: 'folder', id: fid } : { route: 'library', id: null };
  }
  paintNav();
  try {
    if (current.route === 'artifact' && current.id === null) { paint('<p class="empty" data-empty>This artifact is not available.</p>'); return; }
    if (current.route === 'library' || current.route === 'folder') { await reloadLibrary(); viewLibrary(); return; }
    if (current.route !== 'new') await reload();
    ({ mine: viewMine, shared: viewShared, links: viewLinks, usage: viewUsage, new: viewNew,
      artifact: () => viewArtifact(current.id) })[current.route]();
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 401)) paint(V.renderError(describe(e)));
  }
}

async function enter() {
  if (!/^#\//.test(location.hash)) history.replaceState(null, '', `${location.pathname}#/`);
  await route();
}

// ------------------------------------------------------------------------------------------------ uploads
async function runUpload(artifactId, file) {
  const map = uploads.get(artifactId) || new Map();
  uploads.set(artifactId, map);
  const row = { name: file.name, size: file.size, phase: 'queued', done: 0, total: file.size, upload_id: null, controller: null };
  map.set(file.name, row);
  (files.get(artifactId) || files.set(artifactId, new Map()).get(artifactId)).set(file.name, file);
  record(artifactId, file.name, file.size);
  row.controller = new AbortController();
  try {
    const out = await uploadFile(file, {
      client: api, artifactId, signal: row.controller.signal,
      chunksPerRequest: Math.min(MAX_CHUNKS_PER_RUN, Number(cfg.http_chunks_per_request) || MAX_CHUNKS_PER_RUN),
      onProgress: (p) => { Object.assign(row, { phase: p.phase, done: p.done ?? row.done, total: p.total ?? row.total }); paintUploads(artifactId); },
    });
    Object.assign(row, { phase: 'done', upload_id: out.upload_id, error: null });
  } catch (e) {
    if (e?.name === 'AbortError') row.phase = 'paused';
    else Object.assign(row, { phase: 'error', error: describe(e) });
  }
  paintUploads(artifactId);
}

async function startUploads(artifactId, list) {
  for (const file of list) {
    try { await runUpload(artifactId, file); } catch { /* the row shows it */ }
  }
}

// ------------------------------------------------------------------------------------------------ share
function parseList(text) {
  return String(text || '').split(/[,\s]+/).map((s) => s.trim()).filter(Boolean);
}

function parseDays(text, { required }) {
  const raw = String(text ?? '').trim();
  if (!raw) return required ? NaN : undefined;
  const n = Number(raw);
  return Number.isInteger(n) && n >= 1 && n <= MAX_PUBLIC_DAYS ? n : NaN;
}

async function onShareSubmit(form) {
  clearError(form);
  const f = new FormData(form);
  const audience = f.get('audience') === 'public' ? 'public' : 'lab';
  const days = parseDays(f.get(audience === 'public' ? 'public_days' : 'lab_days'), { required: audience === 'public' });
  if (Number.isNaN(days)) return showError(form, `Choose a whole number of days from 1 to ${MAX_PUBLIC_DAYS}.`);
  const body = { version_id: String(f.get('version') || ''), audience };
  if (days !== undefined) body.expires_in_days = days;
  if (audience === 'lab') {
    const groups = parseList(f.get('groups'));
    const emails = parseList(f.get('emails'));
    if (groups.length) body.allow_groups = groups;
    if (emails.length) body.allow_emails = emails;
  }
  const id = current.id;
  try {
    const out = await api.shareCreate(id, body);
    if (out.result !== 'created') return showError(form, out.reason ? `The link could not be created: ${out.reason}` : 'The link could not be created.');
    draft = { artifactId: id, created: out, key: crypto.randomUUID() };
    $('[data-share-region]').innerHTML = V.renderShareSummary(out, { name: memberName() });
    form.hidden = true;
  } catch (e) { showError(form, describe(e)); }
  return undefined;
}

const APPROVE_REFUSALS = {
  not_found: ['This link no longer exists. Create a new one.', false],
  not_pending: ['This link is no longer waiting for approval. Check My links.', false],
  expired: ['This link has expired. Create a new one.', false],
  secret_unavailable: ['The link could not be delivered to you, so it was revoked. Create a new one.', false],
  approval_failed: ['Approving failed. Nothing was published; try Approve again.', true],
};

function setBusy(on) {
  approving = on;
  for (const b of document.querySelectorAll('[data-share-summary] button')) b.disabled = on;
}

async function approve() {
  const region = $('[data-share-region]');
  if (!draft || approving) return;
  const pending = draft;
  const { artifactId, created } = pending;
  setBusy(true);
  try {
    const out = await api.shareApprove(artifactId, created.share_id, { idempotencyKey: pending.key });
    const url = out.secret?.url;
    if (out.result === 'live' && typeof url === 'string' && url) {
      draft = null;
      shownShare = created.share_id;
      await reload();
      region.innerHTML = V.renderSecretUrl(url);
      const form = $('[data-share-form]');
      if (form) form.hidden = false;
      setBusy(false);
      return;
    }
    if (out.result === 'already_approved' || out.result === 'live') {
      // Never replace a URL that is on screen; otherwise the address was already handed out once.
      draft = null;
      if (shownShare !== created.share_id) {
        await reload();
        region.innerHTML = V.renderError(out.result === 'live'
          ? 'The link is live but its address could not be shown. Revoke it in My links and create a new one.'
          : 'This link was already approved; its address is shown only once.');
      }
      setBusy(false);
      return;
    }
    const known = out.result === 'rejected' ? APPROVE_REFUSALS[out.reason] : undefined;
    const [message, again] = known || [out.reason ? `The link could not be approved: ${out.reason}` : 'The link could not be approved.', false];
    if (again) pending.key = crypto.randomUUID();       // the refusal was recorded under the old key; a retry is a new attempt
    else draft = null;
    setBusy(false);
    if (again) { showError(region, message); return; }
    await reload();
    region.innerHTML = V.renderError(message);
    const form = $('[data-share-form]');
    if (form) form.hidden = false;
  } catch (e) {
    setBusy(false);
    showError(region, describe(e));                     // the draft and its key stay: Approve again replays safely
  }
}

async function cancelShare() {
  if (approving) return;
  const region = $('[data-share-region]');
  if (draft) {
    try { await api.shareRevoke(draft.artifactId, draft.created.share_id); } catch { /* the pending link never goes live */ }
  }
  draft = null;
  region.replaceChildren();
  const form = $('[data-share-form]');
  if (form) form.hidden = false;
}

async function copySecret() {
  const input = $('[data-secret-url]');
  if (!input) return;
  try { await navigator.clipboard.writeText(input.value); say('Copied.'); } catch {
    input.select();
    say('Press Ctrl+C to copy.');
  }
}

// ------------------------------------------------------------------------------------------------ actions
async function publish() {
  const id = current.id;
  const rows = [...(uploads.get(id)?.values() || [])];
  if (!rows.length || !rows.every((r) => r.phase === 'done')) return;
  const message = ($('[data-publish-message]')?.value || '').trim() || 'Published from the web';
  const button = $('[data-action="publish"]');
  button.disabled = true;
  try {
    const head = findOwn(id)?.head_version_id || null;
    const out = await api.publish(id, rows.map((r) => ({ name: r.name, upload_id: r.upload_id })), message, head);
    if (out.result === 'created') {
      uploads.delete(id);
      files.delete(id);
      forgetRecord(id);
      say(`Published version ${out.version_number}.`);
      await reload();
      await viewArtifact(id);
    } else if (out.result === 'conflict') {
      showError(app, 'Someone published a newer version first. Reload the page and publish again.');
      button.disabled = false;
    } else {
      showError(app, out.reason ? `Publishing was refused: ${out.reason}` : 'Publishing was refused.');
      button.disabled = false;
    }
  } catch (e) { showError(app, describe(e)); button.disabled = false; }
}

async function createArtifact(form) {
  clearError(form);
  const f = new FormData(form);
  try {
    const out = await api.create(String(f.get('title') || '').trim(), String(f.get('filename') || '').trim(), String(f.get('profile')));
    if (out.result === 'created' && out.artifact_id) { location.hash = `#/a/${encodeURIComponent(out.artifact_id)}`; return; }
    showError(form, out.reason ? `The artifact could not be created: ${out.reason}` : 'The artifact could not be created.');
  } catch (e) { showError(form, describe(e)); }
}

function openDelete() {
  const row = findOwn(current.id);
  if (!row) return;
  busyDelete = row;
  dialogs.innerHTML = V.renderDeleteDialog(row.title || row.artifact_id);
  const dlg = $('dialog', dialogs);
  if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
}

function closeDelete() {
  dialogs.replaceChildren();
  busyDelete = null;
}

async function confirmDelete() {
  const row = busyDelete;
  if (!row) return;
  closeDelete();
  try {
    const out = await api.remove(row.artifact_id);
    if (out.result !== 'deleted') { say(out.reason ? `Delete refused: ${out.reason}` : 'Delete refused.', 'error'); return; }
    forgetRecord(row.artifact_id);
    uploads.delete(row.artifact_id);
    say('Deleted. Its links stopped working.');
    if (location.hash === '#/mine') await route(); else location.hash = '#/mine';
  } catch (e) { say(describe(e), 'error'); }
}

async function revoke(button) {
  const { artifact, share } = button.dataset;
  button.disabled = true;
  try {
    await api.shareRevoke(artifact, share);
    await route();
    say('Link revoked.');
  } catch (e) { say(describe(e), 'error'); button.disabled = false; }
}

const actions = {
  open: (el) => openContent(el),
  accept: (el) => wait(() => api.acceptPending(el.dataset.artifact), 'Added to your library.'),
  discard: (el) => wait(() => api.discardPending(el.dataset.artifact), 'Discarded.'),
  unfile: (el) => wait(() => api.unfileItem(el.dataset.folder, el.dataset.artifact), 'Removed from the folder.'),
  'rename-folder': (el) => folderRename(el),
  'delete-folder': (el) => folderDelete(el),
  signin: () => startSignIn(),
  signout: () => signOut(),
  approve: () => approve(),
  'cancel-share': () => cancelShare(),
  copy: () => copySecret(),
  publish: () => publish(),
  delete: () => openDelete(),
  'cancel-delete': () => closeDelete(),
  'confirm-delete': () => confirmDelete(),
  revoke: (el) => revoke(el),
  pause: (el) => uploads.get(current.id)?.get(el.dataset.name)?.controller?.abort(),
  resume: (el) => {
    const file = files.get(current.id)?.get(el.dataset.name);
    if (file) runUpload(current.id, file);
    else say('Choose this file again to resume it.', 'info');
  },
};

document.addEventListener('click', (ev) => {
  const chip = ev.target instanceof Element ? ev.target.closest('[data-chip]') : null;
  if (chip && chip.dataset.chip in CHIP_SOURCES) { lib.chip = chip.dataset.chip; paintFeed(); return; }
  const el = ev.target instanceof Element ? ev.target.closest('[data-action]') : null;
  const run = el && actions[el.dataset.action];
  if (run) { ev.preventDefault(); Promise.resolve(run(el)).catch((e) => say(describe(e), 'error')); }
});

document.addEventListener('change', (ev) => {
  const t = ev.target;
  if (!(t instanceof Element)) return;
  if (t.matches('[data-file-input]') && current.id) {
    const chosen = [...t.files];
    t.value = '';
    const names = chosen.map((f) => f.name);
    const dup = names.find((n, i) => names.indexOf(n) !== i);
    const busy = chosen.find((f) => ['hashing', 'uploading', 'queued'].includes(uploads.get(current.id)?.get(f.name)?.phase));
    if (dup) say(`Two files are named "${dup}". Rename one and choose them again.`, 'error');
    else if (busy) say(`"${busy.name}" is already uploading.`, 'error');
    else startUploads(current.id, chosen);
  } else if (t.matches('select[data-action="file"]')) {
    const folder = t.value;
    if (folder) {
      wait(() => api.fileItem(folder, t.dataset.artifact), 'Filed.').then((ok) => { if (!ok && t.isConnected) t.value = ''; })
        .catch((e) => say(describe(e), 'error'));
    }
  } else if (t.matches('input[name="audience"]')) {
    const form = t.closest('form');
    const pub = form.elements.audience.value === 'public';
    $('[data-audience-public]', form).hidden = !pub;
    $('[data-audience-lab]', form).hidden = pub;
  }
});

document.addEventListener('input', (ev) => {
  const t = ev.target;
  if (!(t instanceof Element) || !t.matches('[data-search]')) return;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { lib.q = t.value; paintFeed(); }, SEARCH_DEBOUNCE_MS);
});

document.addEventListener('submit', (ev) => {
  const form = ev.target;
  if (!(form instanceof HTMLFormElement)) return;
  if (form.matches('[data-folder-form]')) { ev.preventDefault(); folderCreate(form).catch((e) => say(describe(e), 'error')); return; }
  if (form.matches('[data-share-form]')) { ev.preventDefault(); onShareSubmit(form); }
  else if (form.matches('[data-new-form]')) { ev.preventDefault(); createArtifact(form); }
});

window.addEventListener('hashchange', () => { if (sessions.get()) route(); });

// ------------------------------------------------------------------------------------------------ start
async function main() {
  if (!window.crypto?.subtle || !sessionStore()) { app.innerHTML = V.renderError('This browser cannot sign you in securely.'); return; }
  try {
    const r = await fetch('/api/member/config', { cache: 'no-store', credentials: 'omit' });
    if (!r.ok) throw new Error('config');
    cfg = await r.json();
    if (!validConfig(cfg)) throw new Error('config');
  } catch {
    app.innerHTML = V.renderError('Sign-in is not available right now.');
    return;
  }
  if (location.pathname === '/my/callback') { await handleCallback(); return; }
  if (sessions.get()) { await enter(); return; }
  signedOut('');
}

main();
