// Room file shelf (C5): the room's files, each with its version chain. Every upload is immutable; an edit is a new
// version. Rows are keyed by file id and patched in place, so an open version list or a focused button survives the
// room long-poll. Files are never opened in the page: Download saves them (the server sends them as attachments).
import { patchList } from './keyed-list.js';   // switch to './patch.js' once PR-A's patcher is on this branch

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
export const fileSize = (n) => (n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)} KiB` : `${(n / 1024 / 1024).toFixed(1)} MiB`);
const who = (by, names) => (by ? (names?.(by) || by.id) : '');
const setText = (el, value) => { if (el.textContent !== value) el.textContent = value; };
const setHidden = (el, value) => { if (el.hidden !== value) el.hidden = value; };

function versionRow(v, names) {
  return `<li data-version="${v.version}"><span>v${v.version}</span> · <span>${esc(fileSize(v.size))}</span> · <span>${esc(who(v.by, names))}</span> <button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-download-version="${v.version}" aria-label="Download version ${v.version}">Download</button></li>`;
}

/** mountFilesShelf(host, {onUpload(file), onVersion(row, file), onDownload(row, version?), onDelete(row), names(by)})
 *  -> {update(files), message(text, isError), dispose()} */
export function mountFilesShelf(host, opts = {}) {
  const doc = host.ownerDocument;
  const canUpload = typeof opts.onUpload === 'function';
  host.innerHTML = `<div class="cx-files">
    ${canUpload ? '<div class="cx-row"><button type="button" class="hd-btn hd-btn--sm" data-upload>Upload files</button><input type="file" multiple hidden aria-label="Upload files to the room"></div>' : ''}
    <p class="cx-sub">Agents fetch a file to <code>.collab/files/&lt;name&gt;</code> (the latest version). Every upload is kept; an edit is a new version.</p>
    <p class="cx-msg" data-usage hidden></p>
    <p class="hd-empty" data-empty>No files yet.</p>
    <ul class="cx-list cx-files-list" aria-label="Room files"></ul>
    <span class="cx-msg" role="status" data-files-msg></span></div>`;
  const list = host.querySelector('.cx-files-list'), empty = host.querySelector('[data-empty]'), msg = host.querySelector('[data-files-msg]');
  let rows = new Map();

  const render = (f) => {
    const li = doc.createElement('li');
    li.className = 'cx-file';
    li.dataset.fileId = f.id;
    li.innerHTML = `<div class="cx-row cx-row--between"><div><b data-name></b> <span class="cx-pill" data-version-label></span> <span class="cx-msg" data-size></span>
        <span class="cx-pill" data-kind hidden>download only</span></div>
      <div class="cx-row"><button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-download>Download</button>
        ${opts.onVersion ? '<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-new-version>New version</button><input type="file" hidden data-version-input>' : ''}
        ${opts.onDelete ? '<button type="button" class="hd-btn hd-btn--danger hd-btn--sm" data-delete>Delete</button>' : ''}</div></div>
      <div class="cx-msg"><code data-path></code> · <span data-by></span></div>
      <details><summary>Versions</summary><ol data-versions></ol></details>`;
    update(li, f);
    return li;
  };
  const update = (li, f) => {
    rows.set(f.id, f);
    setText(li.querySelector('[data-name]'), f.name);
    setText(li.querySelector('[data-version-label]'), `v${f.version}`);
    setText(li.querySelector('[data-size]'), fileSize(f.size));
    setText(li.querySelector('[data-path]'), f.path || `.collab/files/${f.name}`);
    setText(li.querySelector('[data-by]'), `by ${who(f.uploaded_by, opts.names)}`);
    const kind = li.querySelector('[data-kind]');
    setHidden(kind, f.kind !== 'executable' && f.kind !== 'archive');
    const del = li.querySelector('[data-delete]');
    if (del) setHidden(del, !f.can_delete);
    const versions = li.querySelector('[data-versions]');
    patchList(versions, [...(f.versions || [])].reverse(), { key: (v) => v.version, render: (v) => versionRow(v, opts.names) });
  };

  const onClick = (event) => {
    const t = event.target;
    if (t.closest('[data-upload]')) { host.querySelector('input[aria-label="Upload files to the room"]').click(); return; }
    const li = t.closest('[data-file-id]');
    if (!li) return;
    const row = rows.get(li.dataset.fileId);
    if (t.closest('[data-download]')) opts.onDownload?.(row);
    else if (t.closest('[data-download-version]')) opts.onDownload?.(row, Number(t.closest('[data-download-version]').dataset.downloadVersion));
    else if (t.closest('[data-new-version]')) li.querySelector('[data-version-input]').click();
    else if (t.closest('[data-delete]')) opts.onDelete?.(row);
  };
  const onChange = (event) => {
    const t = event.target;
    if (!(t instanceof doc.defaultView.HTMLInputElement) || t.type !== 'file' || !t.files?.length) return;
    const picked = Array.from(t.files);
    const li = t.closest('[data-file-id]');
    if (li && t.matches('[data-version-input]')) opts.onVersion?.(rows.get(li.dataset.fileId), picked[0]);
    else if (!li) picked.forEach((f) => opts.onUpload?.(f));
    t.value = '';
  };
  host.addEventListener('click', onClick);
  host.addEventListener('change', onChange);

  return {
    update(files) {
      const next = Array.isArray(files) ? files : [];
      rows = new Map(Array.from(rows).filter(([id]) => next.some((f) => f.id === id)));
      patchList(list, next, { key: (f) => f.id, render, update });
      setHidden(empty, next.length > 0);
    },
    /** usage({used_bytes, limit_bytes}): "used / limit" for the whole room (the room's own limit or the default). */
    usage(u) {
      const el = host.querySelector('[data-usage]');
      const ok = u && Number.isFinite(u.used_bytes) && Number.isFinite(u.limit_bytes);
      setHidden(el, !ok);
      if (ok) setText(el, `Room storage: ${fileSize(u.used_bytes)} used of ${fileSize(u.limit_bytes)}${u.used_bytes > u.limit_bytes ? ' (over the limit: delete files to upload more)' : ''}`);
    },
    message(text, isError = false) {
      setText(msg, text || '');
      if (msg.classList.contains('cx-msg--error') !== isError) msg.classList.toggle('cx-msg--error', isError);
    },
    dispose() { host.removeEventListener('click', onClick); host.removeEventListener('change', onChange); },
  };
}

const MIB = 1024 * 1024;

/** mountRoomProperties(host, {save(body) -> settings}) -> {update(settings), dispose()}
 *  The room's properties panel: its own total file-storage limit, in MiB (empty = the operator's default). Only a
 *  room owner (settings.can_edit) can change it; the server re-checks that and the ceiling. */
export function mountRoomProperties(host, opts = {}) {
  host.innerHTML = `<section class="hd-card cx-stack" aria-label="Room properties"><div class="hd-card__title">Room properties</div>
    <form class="cx-stack" data-props novalidate>
      <label class="cx-field">File storage limit (MiB)<input class="cx-input" type="number" name="files_room_mib" min="1" step="1" inputmode="numeric" aria-describedby="hcn-props-usage"></label>
      <p class="cx-sub" id="hcn-props-usage" data-props-usage></p>
      <div class="cx-row" data-props-actions><button type="submit" class="hd-btn hd-btn--sm" aria-label="Save room properties">Save</button></div>
      <span class="cx-msg" role="status" data-props-msg></span></form></section>`;
  const form = host.querySelector('[data-props]'), field = form.elements.files_room_mib;
  const msg = host.querySelector('[data-props-msg]'), info = host.querySelector('[data-props-usage]');
  let cur = null;
  const doc = () => host.ownerDocument;
  const say = (text, isError = false) => { setText(msg, text); msg.classList.toggle('cx-msg--error', isError); };
  const maxMib = () => Math.floor((cur?.files_room_bytes_max || 0) / MIB);
  const paint = () => {
    if (!cur) return;
    const own = cur.files_room_bytes;
    setText(info, `${fileSize(cur.files_used_bytes || 0)} used · limit ${fileSize(cur.files_room_bytes_effective)}${own == null ? ' (default)' : ''} · at most ${maxMib()} MiB · empty = the default (${fileSize(cur.files_room_bytes_default)})`);
    field.max = String(maxMib());
    field.placeholder = `default: ${Math.round(cur.files_room_bytes_default / MIB)}`;
    if (doc().activeElement !== field) field.value = own == null ? '' : String(Math.round(own / MIB));
    field.disabled = !cur.can_edit;
    const actions = host.querySelector('[data-props-actions]');
    if (!cur.can_edit && actions) actions.remove();
  };
  const onSubmit = async (event) => {
    event.preventDefault();
    if (!cur?.can_edit) return;
    const raw = field.value.trim();
    let bytes = null;
    if (raw !== '') {
      const n = Number(raw);
      if (!/^\d+$/.test(raw) || !Number.isInteger(n) || n < 1 || n > maxMib()) {
        field.setAttribute('aria-invalid', 'true');
        say(`Enter a whole number of MiB from 1 to ${maxMib()}, or leave it empty for the default.`, true);
        return;
      }
      bytes = n * MIB;
    }
    field.setAttribute('aria-invalid', 'false');
    try {
      const next = await opts.save?.({ files_room_bytes: bytes });
      if (next) cur = next;
      paint();
      const below = bytes != null && bytes < (cur.files_used_bytes || 0);
      say(below ? 'Saved. The room already uses more than this: nothing is deleted, but new uploads are blocked until files are deleted.' : 'Saved.');
    } catch (e) { say(e.message || 'Could not save.', true); }
  };
  form.addEventListener('submit', onSubmit);
  return {
    update(settings) { cur = settings; paint(); },
    dispose() { form.removeEventListener('submit', onSubmit); },
  };
}
