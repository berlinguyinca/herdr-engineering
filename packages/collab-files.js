// Shared room-file surface. Server records stay authoritative; upload manifests
// and grants are never promoted into room files by the browser.
import { escapeHtml as esc } from './herdr-web-components/util.js';

const number = value => Math.max(0, Number.isFinite(Number(value)) ? Number(value) : 0);
const bytes = value => `${number(value)} bytes`;
const button = (label, attrs = '') => `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" ${attrs}>${esc(label)}</button>`;

export function renderFileBrowser(files, state = {}) {
  if (state.error) return `<p role="alert">${esc(state.error)}</p>`;
  if (state.status === 'loading') return '<p role="status">Loading files…</p>';
  const path = state.path || '', query = (state.query || '').trim().toLowerCase();
  const prefix = path ? `${path}/` : '';
  const available = files.filter(file => file.status === 'available' && typeof file.relative_path === 'string');
  const crumbs = [button('All files', 'data-file-path=""')];
  path.split('/').filter(Boolean).forEach((part, i, parts) => crumbs.push(button(part, `data-file-path="${esc(parts.slice(0, i + 1).join('/'))}"`)));
  const folders = new Set(), visible = [];
  for (const file of available) {
    if (!file.relative_path.startsWith(prefix)) continue;
    if (query) { if (file.relative_path.toLowerCase().includes(query)) visible.push(file); continue; }
    const relative = file.relative_path.slice(prefix.length);
    if (relative.includes('/')) folders.add(prefix + relative.split('/')[0]);
    else visible.push(file);
  }
  const rows = [...folders].sort().map(folder => `<li>${button(folder.slice(prefix.length) + '/', `data-file-path="${esc(folder)}"`)}</li>`);
  rows.push(...visible.map(file => `<li data-file-id="${esc(file.file_id)}"><span class="cx-file-name"><b>${esc(query ? file.relative_path : file.relative_path.slice(prefix.length))}</b><small>${bytes(file.size_bytes)} · ${esc(file.status)}${file.created_at ? ` · ${esc(file.created_at)}` : ''}${file.preview_status ? ` · Preview ${esc(file.preview_status)}` : ''}</small></span><span class="cx-row">${button('Preview', `data-file-action="preview"`)}${button('Download', `data-file-action="download"`)}</span></li>`));
  return `<nav class="cx-row" aria-label="File path">${crumbs.join(' / ')}</nav>${rows.length ? `<ul class="cx-list cx-file-list">${rows.join('')}</ul>` : `<p class="hd-empty">${query ? 'No files match this search.' : 'No files in this path.'}</p>`}`;
}

export function renderUploadBatch(batch) {
  const total = number(batch.total_bytes), done = number(batch.uploaded_bytes);
  const percent = total ? Math.min(100, Math.round(done / total * 100)) : 0;
  return `<div class="cx-upload-batch"><b>${esc(batch.upload_id || 'Upload')}</b><span>${esc(batch.status || 'created')} · ${number(batch.file_count)} files · ${bytes(done)} / ${bytes(total)} · ${number(batch.completed_parts)} / ${number(batch.total_parts)} parts</span><progress aria-label="Upload progress" max="100" value="${percent}">${percent}%</progress>${batch.status === 'failed' ? '<span>Upload failed. Resume or retry through your harness.</span>' : batch.status === 'available' ? '<span>Finalized files are available in the room.</span>' : '<span>Files remain hidden until verification and finalization complete.</span>'}</div>`;
}

export async function openAuthorizedFile(file, { api, purpose = 'download' } = {}) {
  if (file.status !== 'available') throw new Error('Only finalized, available files can be opened.');
  if (!['download', 'preview'].includes(purpose)) throw new Error('Invalid file purpose.');
  const source = await api.fileAccess(file.room_id, file.file_id, purpose);
  let url;
  try { url = new URL(source.url); } catch { throw new Error('Invalid authorized file source.'); }
  if (url.protocol !== 'https:' || url.username || url.password) throw new Error('A secure authorized file source is required.');
  if (!Number.isFinite(source.expires_at) || source.expires_at * 1000 <= Date.now()) throw new Error('Authorized file source expired. Try again.');
  return source;
}

// `pattern` is the locally expanded pattern manifest used by CollabUploads.
// The browser cannot expand paths on a remote harness or read its file bytes.
export async function startBulkUploadPreview(roomId, pattern, { api } = {}) {
  if (!pattern || !Array.isArray(pattern.files) || !pattern.files.length) throw new Error('Provide a harness-expanded upload manifest.');
  return api.previewUpload(roomId, pattern);
}

export function mountFilesPanel(root, { api, roomId, info, isCurrent, pending, onPermissions }) {
  const state = { path: '', query: '', files: [], next: null, batches: new Map(), status: 'loading', error: '' };
  let alive = true, loading = false, lastRefresh = 0, pageCount = 1;
  const current = () => alive && isCurrent();
  const key = `upload:${roomId}`;
  const find = selector => root.querySelector(selector);
  root.innerHTML = `<div class="cx-row cx-row--between"><h2 class="hd-card__title">Room files</h2>${button('Refresh files', 'data-refresh-files')}${info.capabilities.canInvite ? button('Manage file permissions', 'data-file-permissions') : ''}</div>
    <label class="cx-field">Search loaded files<input type="search" data-file-search></label>
    <div data-file-list></div>${button('Load more files', 'data-more-files hidden')}
    <p data-file-message role="status" class="cx-msg"></p>
    <div data-file-preview hidden class="cx-stack"></div>
    <h3 class="hd-card__title">Direct uploads</h3>
    <p class="cx-sub">Expand your file pattern locally with your harness and paste its checksum manifest. Preview here, then give the room and manifest instructions below to your registered harness agent. The agent creates the upload and transfers the bytes.</p>
    <p data-upload-permission class="cx-msg"></p>
    <form data-upload-preview class="cx-stack"><label class="cx-field">Upload manifest<textarea name="manifest" rows="3" required spellcheck="false" placeholder='{"files":[…]}'></textarea></label><button class="hd-btn" type="submit">Preview upload</button></form>
    <div data-upload-summary role="status"></div><div data-upload-handoff class="cx-stack"></div>
    <p data-upload-message role="status" class="cx-msg"></p>
    <form data-upload-watch class="cx-row"><label class="cx-field">Agent-created upload ID<input name="upload" required></label><button class="hd-btn hd-btn--ghost" type="submit">Track upload</button></form>
    <p data-upload-status-loading class="cx-msg" role="status"></p>
    <p data-upload-status-error class="cx-msg" role="status"></p>
    <div data-upload-batches aria-live="polite"></div>`;
  const rendered = new Map();
  const put = (selector, html) => {
    const el = find(selector);
    if (rendered.get(selector) !== html) { el.innerHTML = html; rendered.set(selector, html); }
  };
  const message = (selector, text, error = false) => {
    if (!current()) return;
    const el = find(selector); el.setAttribute('role', error ? 'alert' : 'status'); el.textContent = text;
  };
  const controls = () => {
    if (!current()) return;
    const allowed = info.capabilities.canUpload;
    find('[data-upload-permission]').textContent = allowed ? 'You can upload files to this room.' : 'Ask the room owner to enable uploads for your membership.';
    find('[data-upload-preview] button').disabled = pending.has(key);
    find('[name="manifest"]').disabled = pending.has(key);
  };
  const draw = () => {
    if (!current()) return;
    put('[data-file-list]', renderFileBrowser(state.files, state));
    find('[data-more-files]').hidden = state.next == null;
    find('[data-more-files]').disabled = loading;
    find('[data-refresh-files]').disabled = loading;
    put('[data-upload-batches]', [...state.batches.values()].map(renderUploadBatch).join(''));
    controls();
  };
  const acceptBatch = batch => {
    if (batch.room_id !== roomId || !batch.upload_id) throw new Error('Invalid upload status for this room.');
    // Retain only aggregate status; grants and per-file transfer details never enter UI state.
    const { upload_id, room_id, status, file_count, total_bytes, uploaded_bytes, completed_parts, total_parts } = batch;
    state.batches.set(upload_id, { upload_id, room_id, status, file_count, total_bytes, uploaded_bytes, completed_parts, total_parts });
  };
  // Status requests have their own per-upload lifetimes. A stalled provider
  // cannot hold file rendering, pagination, or refresh controls hostage.
  const statusRequests = new Map(), statusErrors = new Map();
  const drawStatus = () => {
    if (!current()) return;
    put('[data-upload-batches]', [...state.batches.values()].map(renderUploadBatch).join(''));
    message('[data-upload-status-loading]', statusRequests.size ? 'Refreshing upload status…' : '');
    message('[data-upload-status-error]', [...statusErrors.values()].join(' '), statusErrors.size > 0);
  };
  const loadStatus = uploadId => {
    if (statusRequests.has(uploadId)) return statusRequests.get(uploadId);
    const request = (async () => {
      try {
        const batch = await api.uploadStatus(uploadId);
        if (!current()) return;
        acceptBatch(batch); statusErrors.delete(uploadId);
      } catch (error) {
        if (current() && error.name !== 'AbortError') statusErrors.set(uploadId, `${error.message} Upload progress may be stale.`);
        throw error;
      } finally {
        statusRequests.delete(uploadId);
        drawStatus();
      }
    })();
    statusRequests.set(uploadId, request); drawStatus();
    return request;
  };
  const refresh = async (force = false, more = false) => {
    if (!current() || loading || (!force && Date.now() - lastRefresh < 3000)) return;
    loading = true; lastRefresh = Date.now(); draw();
    try {
      // Refresh capabilities too: role alone is never an upload grant.
      const room = await api.room(roomId);
      if (!current()) return;
      info = room; controls();
      const records = new Map((more ? state.files : []).map(file => [file.file_id, file]));
      let cursor = more ? state.next : undefined, loaded = 0;
      do {
        const data = await api.roomFiles(roomId, cursor);
        if (!current()) return;
        if (!Array.isArray(data.files) || data.files.some(file => !file || file.room_id !== roomId || !file.file_id || typeof file.relative_path !== 'string')) throw new Error('Invalid room file response.');
        data.files.forEach(file => records.set(file.file_id, file));
        cursor = data.has_more ? data.next_cursor : null;
        for (const batch of data.uploads || []) acceptBatch(batch);
        loaded += 1;
      } while (!more && cursor != null && loaded < pageCount);
      pageCount = more ? pageCount + 1 : loaded;
      state.files = [...records.values()]; state.next = cursor;
      state.error = ''; state.status = 'ready';
    } catch (error) {
      if (current() && error.name !== 'AbortError') { state.error = error.message; state.status = 'error'; }
    } finally { loading = false; if (current()) draw(); }
    if (current()) for (const batch of state.batches.values()) {
      if (!['available', 'failed'].includes(batch.status)) loadStatus(batch.upload_id).catch(() => {});
    }
  };
  find('[data-file-search]').oninput = event => { if (current()) { state.query = event.target.value; draw(); } };
  find('[name="manifest"]').oninput = () => { if (!current()) return; put('[data-upload-handoff]', ''); put('[data-upload-summary]', ''); controls(); };
  root.onclick = async event => {
    const target = event.target.closest('button');
    if (!current() || !target || target.disabled) return;
    if (target.hasAttribute('data-file-path')) { state.path = target.dataset.filePath; draw(); }
    else if (target.hasAttribute('data-refresh-files')) await refresh(true);
    else if (target.hasAttribute('data-more-files')) await refresh(true, true);
    else if (target.hasAttribute('data-file-permissions')) onPermissions();
    else if (target.dataset.fileAction) {
      const file = state.files.find(file => file.file_id === target.closest('[data-file-id]')?.dataset.fileId);
      if (!file) return;
      const accessKey = `file:${roomId}:${file.file_id}`;
      if (pending.has(accessKey)) return;
      pending.add(accessKey); target.disabled = true;
      message('[data-file-message]', 'Authorizing file access…');
      try {
        const source = await openAuthorizedFile(file, { api, purpose: target.dataset.fileAction });
        if (!current()) return;
        if (target.dataset.fileAction === 'download') {
          const link = document.createElement('a');
          link.href = source.url; link.download = file.relative_path.split('/').at(-1); link.rel = 'noopener noreferrer';
          link.referrerPolicy = 'no-referrer'; root.append(link); link.click(); link.remove();
          message('[data-file-message]', 'Download authorized.');
        } else {
          const detail = find('[data-file-preview]'); detail.hidden = false;
          detail.textContent = `Preview authorized: ${file.relative_path} · ${bytes(file.size_bytes)}. Specialized preview rendering is not available yet; use Download to open the file locally.`;
          message('[data-file-message]', '');
        }
      } catch (error) { if (error.name !== 'AbortError') message('[data-file-message]', error.message, true); }
      finally { pending.delete(accessKey); if (current()) target.disabled = false; }
    }
  };
  find('[data-upload-preview]').onsubmit = async event => {
    event.preventDefault();
    if (!current() || pending.has(key)) return;
    put('[data-upload-handoff]', ''); pending.add(key); controls();
    put('[data-upload-summary]', ''); message('[data-upload-message]', 'Checking upload…');
    try {
      const candidate = JSON.parse(find('[name="manifest"]').value);
      const result = await startBulkUploadPreview(roomId, candidate, { api });
      if (!current()) return;
      if (result.room_id !== roomId || !Number.isSafeInteger(result.file_count) || result.file_count < 1 || !Number.isFinite(result.total_bytes) || result.total_bytes < 0) throw new Error('Invalid upload preview.');
      put('[data-upload-summary]', `${result.file_count} files · ${bytes(result.total_bytes)}`);
      const request = { name: 'room_upload_start', arguments: { room_id: roomId, manifest: candidate, options: {} } };
      put('[data-upload-handoff]', `<p>Room: ${esc(roomId)}. Give this manifest to your registered harness agent. The agent must check its own room upload permission and call room_upload_start through MCP. Use that same registered agent to resume, check status, transfer bytes, and finalize with room_upload_finalize. Paste its returned upload ID below to track progress here.</p><label class="cx-field">Agent upload instructions<textarea readonly rows="6" spellcheck="false">${esc(JSON.stringify(request, null, 2))}</textarea></label>`);
      message('[data-upload-message]', result.can_upload === true ? 'Preview ready. No upload session was created. Your agent must obtain its own authorization.' : 'Ask the room owner to enable uploads for your membership.');
    } catch (error) { if (error.name !== 'AbortError') message('[data-upload-message]', error.message, true); }
    finally { pending.delete(key); controls(); }
  };
  find('[data-upload-watch]').onsubmit = async event => {
    event.preventDefault();
    const submit = find('[data-upload-watch] button');
    if (!current() || submit.disabled) return;
    submit.disabled = true;
    try {
      await loadStatus(find('[name="upload"]').value.trim());
      if (!current()) return;
      message('[data-upload-message]', 'Tracking upload.');
    } catch (error) { if (error.name !== 'AbortError') message('[data-upload-message]', error.message, true); }
    finally { if (current()) submit.disabled = false; }
  };
  draw();
  return { refresh, dispose: () => { alive = false; root.onclick = null; } };
}
