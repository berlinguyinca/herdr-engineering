// The dashboard's artifact library: the artifact host's feed and folders, read through /api/v1/library. READ-ONLY:
// no approve, revoke, file or delete control exists here (those stay on the member site). Rows, chips and the folder
// sidebar are the member site's own pure renderers (in their read-only form); a row opens #/library/<id>, whose page
// shows the item's versions, files and shares and frames one file inside a fully sandboxed iframe (library-frame.js).
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { skeleton } from './motion.js';
import { filterFeed, renderChips, renderFeedList, renderFolderSidebar, formatDate, humanBytes } from './member-artifacts-view.js';
import { mountLibraryFrame } from './library-frame.js';

export const SEARCH_DEBOUNCE_MS = 150;
const PAGE = 200;
const ALL_SOURCES = ['mine', 'assigned', 'opened', 'pending'];
const CHIP_SOURCES = { all: ['mine', 'assigned', 'opened'], assigned: ['assigned'], opened: ['opened'], mine: ['mine'], pending: ['pending'] };

/** Expired and revoked rows have no link; everything else opens the library item page. */
export const rowHref = (row) => (row.status === 'expired' || row.status === 'revoked' ? null : `#/library/${encodeURIComponent(row.artifact_id)}`);
export const folderHref = (id) => `#/library?folder=${encodeURIComponent(id)}`;

/** The server's refusal text when it sent one (e.g. the pending cap), else the error message. */
export const describe = (error) => String(error?.body?.message || error?.message || 'That did not work.');

export function chipCounts(items) {
  const counts = { all: filterFeed(items, { sources: CHIP_SOURCES.all }).length };
  for (const key of ['assigned', 'opened', 'mine', 'pending']) counts[key] = filterFeed(items, { sources: CHIP_SOURCES[key] }).length;
  return counts;
}

/** The page body for one state: pure, so it can be checked without a DOM. */
export function renderLibrary({ items = [], folders = [], chip = 'all', q = '', folderId = null, total = null, more = false } = {}) {
  const folder = folderId ? folders.find((f) => f.folder_id === folderId) : null;
  if (folderId && !folder) return '<p class="empty" data-empty>This folder is not available.</p>';
  const shown = filterFeed(items, { sources: CHIP_SOURCES[chip] || CHIP_SOURCES.all });
  return `<div class="library" data-library>${renderFolderSidebar(folders, folder ? folder.folder_id : null, { allHref: '#/library', folderHref, readOnly: true })}
<section class="library-main"><h2>${folder ? esc(folder.name) : 'Library'}</h2>
<label class="search">Search <input type="search" class="hd-input" data-search maxlength="200" autocomplete="off" placeholder="Title, sharer, folder" value="${esc(q)}"></label>
<div data-chips-region>${renderChips(chipCounts(items), chip)}</div>
<p data-library-state role="status"></p>
<div data-feed-region>${renderFeedList(shown, { folders, activeFolder: folder ? folder.folder_id : null, href: rowHref, readOnly: true })}</div>
${more ? `<button type="button" class="btn" data-action="more">Load more${total === null ? '' : ` (${esc(total)} in all)`}</button>` : ''}
</section></div>`;
}

export function mountLibrary(container, client, { freshness = () => null, folderId = null } = {}) {
  let disposed = false, controller, generation = 0, searchTimer = null;
  const state = { items: [], folders: [], chip: 'all', q: '', folderId, total: null, next: null, loaded: false };
  const say = (text) => { const el = container.querySelector('[data-library-state]'); if (el) el.textContent = text; };

  const load = async ({ append = false } = {}) => {
    if (disposed) return;
    controller?.abort();
    const request = new AbortController(); controller = request;
    const mine = ++generation;
    const signal = AbortSignal.any([request.signal, AbortSignal.timeout(15000)]);
    if (!state.loaded) container.innerHTML = skeleton('library');
    try {
      const page = await client.listLibrary({ q: state.q, sources: ALL_SOURCES, folderId: state.folderId, limit: PAGE, offset: append ? state.items.length : 0, signal });
      if (disposed || mine !== generation) return;
      if (!page || !Array.isArray(page.items) || !Array.isArray(page.folders)) throw new Error('Invalid response');
      state.items = append ? [...state.items, ...page.items] : page.items;
      state.folders = page.folders; state.total = page.total; state.next = page.next_offset;
      state.loaded = true;
      paint();
      freshness()?.touch(true);
    } catch (error) {
      if (disposed || mine !== generation || request.signal.aborted) return;
      if (!state.loaded) container.innerHTML = `<herdr-error message="${esc(describe(error))}"></herdr-error><button type="button" class="btn" data-action="retry">Retry</button>`;
      else say(describe(error));
      freshness()?.touch(false, describe(error));
    }
  };

  const paint = () => {
    const search = container.querySelector('[data-search]');
    const focused = search && document.activeElement === search;
    container.innerHTML = renderLibrary({ ...state, more: state.next !== null && state.next !== undefined });
    if (focused) { const fresh = container.querySelector('[data-search]'); fresh.focus(); fresh.setSelectionRange(fresh.value.length, fresh.value.length); }
  };

  const onClick = (event) => {
    const el = event.target.closest('[data-action],[data-chip]');
    if (!el || !container.contains(el)) return;
    if (el.dataset.chip) { state.chip = el.dataset.chip; paint(); return; }
    if (el.dataset.action === 'more') void load({ append: true });
    else if (el.dataset.action === 'retry') void load();
  };
  const onInput = (event) => {
    const t = event.target;
    if (!t.matches?.('[data-search]')) return;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.q = t.value; void load(); }, SEARCH_DEBOUNCE_MS);
  };
  container.addEventListener('click', onClick);
  container.addEventListener('input', onInput);
  void load();
  return {
    refresh() { if (state.loaded) void load(); },
    dispose() {
      disposed = true; controller?.abort(); clearTimeout(searchTimer);
      for (const [type, fn] of [['click', onClick], ['input', onInput]]) container.removeEventListener(type, fn);
    },
  };
}

const when = (iso) => (iso ? esc(formatDate(iso)) : 'never');
const filesList = (files = []) => (files.length
  ? `<ul class="library-files">${files.map((f) => `<li><button type="button" class="btn" data-action="open-file" data-path="${esc(f.path)}">${esc(f.path)}</button> <span class="muted">${esc(f.media_type || '')} ${f.size == null ? '' : esc(humanBytes(f.size))}</span></li>`).join('')}</ul>`
  : '<p class="empty">No files.</p>');

/** The item page body: pure. Every control here only reads (open a version, open a file in the sandboxed frame). */
export function renderLibraryItem({ item = null, versions = [], shares = [], versionId = null } = {}) {
  if (!item) return '<p class="empty" data-empty>This item is not available.</p>';
  const current = versionId || item.current_version_id || versions.find((v) => v.current)?.version_id || versions[0]?.version_id;
  const shown = versions.find((v) => v.version_id === current);
  const files = shown ? shown.files : item.files;
  return `<div class="library-item" data-library-item="${esc(item.artifact_id)}"><h2>${esc(item.title || item.artifact_id)}</h2>
<p class="muted">Owner ${esc(item.owner || 'unknown')} · ${esc(item.artifact_type || 'file')} · updated ${when(item.updated_at)}</p>
<section><h3>Versions</h3><ul class="library-versions">${versions.map((v) => `<li${v.version_id === current ? ' aria-current="true"' : ''}><a href="#/library/${encodeURIComponent(item.artifact_id)}?version=${encodeURIComponent(v.version_id)}">Version ${esc(v.version_number)}</a> <span class="muted">${esc(v.state)}${v.current ? ' (current)' : ''} · ${when(v.created_at)}</span></li>`).join('') || '<li class="empty">No versions.</li>'}</ul></section>
<section><h3>Files</h3>${filesList(files)}</section>
<section><h3>Shares</h3><ul class="library-shares">${shares.map((s) => `<li>${esc(s.audience)} · ${esc(s.state)}${s.active ? ' (active)' : ''} · expires ${when(s.expires_at)}</li>`).join('') || '<li class="empty">No shares.</li>'}</ul></section>
<section><h3>Preview</h3><div data-frame-slot><p class="muted">Open a file to preview it. It runs in a sandbox with no scripts.</p></div></section></div>`;
}

/** The frame guard takes origin-relative paths only: a same-origin absolute URL (a client with a base) is cut to its path. */
const originPath = (url) => {
  try { const u = new URL(url, globalThis.location?.origin); return u.origin === globalThis.location?.origin ? u.pathname + u.search : url; } catch { return url; }
};

export function mountLibraryItem(container, client, itemId, { freshness = () => null, versionId = null } = {}) {
  let disposed = false;
  const controller = new AbortController();
  const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]);
  const state = { item: null, versions: [], shares: [] };
  const paint = () => { container.innerHTML = renderLibraryItem({ ...state, versionId }); };
  const load = async () => {
    container.innerHTML = skeleton('library item', { rows: 2, height: 40 });
    try {
      const [item, versions, shares] = await Promise.all([client.libraryItem(itemId, { signal }), client.libraryVersions(itemId, { signal }), client.libraryShares(itemId, { signal })]);
      if (disposed) return;
      state.item = item?.item || null; state.versions = versions?.versions || []; state.shares = shares?.shares || [];
      paint(); freshness()?.touch(true);
    } catch (error) {
      if (disposed || controller.signal.aborted) return;
      container.innerHTML = `<herdr-error message="${esc(describe(error))}"></herdr-error><button type="button" class="btn" data-action="retry">Retry</button>`;
      freshness()?.touch(false, describe(error));
    }
  };
  const onClick = (event) => {
    const el = event.target.closest('[data-action]');
    if (!el || !container.contains(el)) return;
    if (el.dataset.action === 'retry') { void load(); return; }
    if (el.dataset.action !== 'open-file' || !state.item) return;
    const slot = container.querySelector('[data-frame-slot]');
    const version = versionId || state.item.current_version_id || state.versions.find((v) => v.current)?.version_id || state.versions[0]?.version_id;
    try { mountLibraryFrame(slot, { url: originPath(client.libraryContentUrl(state.item.artifact_id, version, el.dataset.path)), title: el.dataset.path }); }
    catch (error) { slot.textContent = describe(error); }
  };
  container.addEventListener('click', onClick);
  void load();
  return { refresh() { void load(); }, dispose() { disposed = true; controller.abort(); container.removeEventListener('click', onClick); } };
}
