// The dashboard's artifact library: the same feed, folders and pending inbox as the member site, read through
// /api/v1/library. Rows, chips and the folder sidebar are the member site's own pure renderers, so both surfaces
// draw the same markup for the same data; only the open link (the dashboard's artifact page) differs.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { skeleton } from './motion.js';
import { filterFeed, renderChips, renderFeedList, renderFolderSidebar } from './member-artifacts-view.js';

export const SEARCH_DEBOUNCE_MS = 150;
const PAGE = 200;
const ALL_SOURCES = ['mine', 'assigned', 'opened', 'pending'];
const CHIP_SOURCES = { all: ['mine', 'assigned', 'opened'], assigned: ['assigned'], opened: ['opened'], mine: ['mine'], pending: ['pending'] };

/** Expired and revoked rows have no link, nor has a row the server says this key cannot read (`readable: false`, sent
 * for a key mapped to a member); everything else opens the dashboard's artifact page. */
export const rowHref = (row) => (row.status === 'expired' || row.status === 'revoked' || row.readable === false ? null : `#/artifacts/${encodeURIComponent(row.artifact_id)}`);
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
  return `<div class="library" data-library>${renderFolderSidebar(folders, folder ? folder.folder_id : null, { allHref: '#/library', folderHref })}
<section class="library-main"><h2>${folder ? esc(folder.name) : 'Library'}</h2>
<label class="search">Search <input type="search" class="hd-input" data-search maxlength="200" autocomplete="off" placeholder="Title, sharer, folder" value="${esc(q)}"></label>
<div data-chips-region>${renderChips(chipCounts(items), chip)}</div>
<p data-library-state role="status"></p>
<div data-feed-region>${renderFeedList(shown, { folders, activeFolder: folder ? folder.folder_id : null, href: rowHref })}</div>
${more ? `<button type="button" class="btn" data-action="more">Load more${total === null ? '' : ` (${esc(total)} in all)`}</button>` : ''}
</section></div>`;
}

export function mountLibrary(container, client, { freshness = () => null, folderId = null, confirm = (m) => window.confirm(m), prompt = (m, v) => window.prompt(m, v) } = {}) {
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
      freshness(true);
    } catch (error) {
      if (disposed || mine !== generation || request.signal.aborted) return;
      if (!state.loaded) container.innerHTML = `<herdr-error message="${esc(describe(error))}"></herdr-error><button type="button" class="btn" data-action="retry">Retry</button>`;
      else say(describe(error));
      freshness(false, describe(error));
    }
  };

  const paint = () => {
    const search = container.querySelector('[data-search]');
    const focused = search && document.activeElement === search;
    container.innerHTML = renderLibrary({ ...state, more: state.next !== null && state.next !== undefined });
    if (focused) { const fresh = container.querySelector('[data-search]'); fresh.focus(); fresh.setSelectionRange(fresh.value.length, fresh.value.length); }
  };

  /** Run one write, reload, and say what happened (a refusal shows the server's message). */
  const act = async (run, done) => {
    say('Working…');
    try { await run(); } catch (error) { say(describe(error)); return false; }
    await load();
    say(done);
    return true;
  };

  const folderOf = (id) => state.folders.find((f) => f.folder_id === id);
  const onClick = (event) => {
    const el = event.target.closest('[data-action],[data-chip]');
    if (!el || !container.contains(el)) return;
    if (el.dataset.chip) { state.chip = el.dataset.chip; paint(); return; }
    const id = el.dataset.artifact;
    switch (el.dataset.action) {
      case 'accept': void act(() => client.acceptPending(id), 'Added to your library.'); break;
      case 'discard': if (confirm('Discard this pushed artifact? It is deleted.')) void act(() => client.discardPending(id), 'Discarded.'); break;
      case 'unfile': void act(() => client.unfileItem(el.dataset.folder, id), 'Removed from the folder.'); break;
      case 'more': void load({ append: true }); break;
      case 'retry': void load(); break;
      case 'rename-folder': {
        const folder = folderOf(el.dataset.folder);
        const name = String(prompt('Folder name', folder?.name || '') || '').trim();
        if (name && folder && name !== folder.name) void act(() => client.renameFolder(folder.folder_id, name), 'Folder renamed.');
        break;
      }
      case 'delete-folder': {
        const folder = folderOf(el.dataset.folder);
        if (!folder || !confirm(`Delete the folder "${folder.name}"? Its items stay in your library.`)) break;
        void act(() => client.deleteFolder(folder.folder_id), 'Folder deleted.').then((ok) => { if (ok && state.folderId === folder.folder_id) window.location.hash = '#/library'; });
        break;
      }
      default:
    }
  };
  const onInput = (event) => {
    const t = event.target;
    if (!t.matches?.('[data-search]')) return;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.q = t.value; void load(); }, SEARCH_DEBOUNCE_MS);
  };
  const onChange = (event) => {
    const t = event.target;
    if (!t.matches?.('select[data-action="file"]') || !t.value) return;
    void act(() => client.fileItem(t.value, t.dataset.artifact), 'Filed.');
  };
  const onSubmit = (event) => {
    const form = event.target;
    if (!form.matches?.('[data-folder-form]')) return;
    event.preventDefault();
    const name = String(new FormData(form).get('name') || '').trim();
    if (name) void act(() => client.createFolder(name), 'Folder created.');
  };
  container.addEventListener('click', onClick);
  container.addEventListener('input', onInput);
  container.addEventListener('change', onChange);
  container.addEventListener('submit', onSubmit);
  void load();
  return {
    refresh() { if (state.loaded) void load(); },
    dispose() {
      disposed = true; controller?.abort(); clearTimeout(searchTimer);
      for (const [type, fn] of [['click', onClick], ['input', onInput], ['change', onChange], ['submit', onSubmit]]) container.removeEventListener(type, fn);
    },
  };
}
