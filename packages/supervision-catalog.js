// One-room-at-a-time catalog shared by Collaboration, Agents and Assistants.
// The server owns authorization and projection; this view never joins agents or
// infers ancestry from runtime layout.
import {escapeHtml as esc} from './herdr-web-components/util.js';
import {mountSupervisionTree} from './supervision-tree.js';

const text = value => typeof value === 'string' ? value.trim() : '';
const records = value => Array.isArray(value)
  ? value.filter(item => item && typeof item === 'object' && !Array.isArray(item))
  : [];

export function selectCatalogRooms(payload = {}) {
  const seen = new Set();
  return records(payload.rooms).filter(room => {
    const id = text(room.room_id);
    if (!id || text(room.role) === 'guest' || seen.has(id)) return false;
    seen.add(id); return true;
  }).map(room => ({room_id: text(room.room_id), name: text(room.name) || text(room.room_id), role: text(room.role)}))
    .sort((a, b) => a.name.localeCompare(b.name) || a.room_id.localeCompare(b.room_id));
}

const styles = `<style>
.sc-head{display:flex;flex-wrap:wrap;gap:12px;align-items:end;justify-content:space-between}.sc-head h2{margin:0;overflow-wrap:anywhere}.sc-hierarchy,.sc-status{color:var(--text-secondary);font-size:12px;line-height:1.5;overflow-wrap:anywhere}.sc-room{display:flex;min-width:min(260px,100%);flex-direction:column;gap:4px;font-size:12px}.sc-room select{box-sizing:border-box;max-width:100%;padding:7px 9px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--surface);color:var(--text);font:inherit}.sc-status[data-state=error]{color:var(--status-error,#b42318)}.sc-tree{min-width:0}
@media(max-width:650px){.sc-head{align-items:stretch;flex-direction:column}.sc-room{min-width:0;width:100%}}
</style>`;

export function mountSupervisionCatalog(container, client, {
  title = 'Job trees', roomId = '', pollMs = 7000,
  documentImpl = globalThis.document,
  schedule = globalThis.setTimeout,
  cancelSchedule = globalThis.clearTimeout,
  treeMount = mountSupervisionTree,
} = {}) {
  let disposed = false;
  let rooms = [];
  let selectedRoom = text(roomId);
  let request = null;
  let roomRequest = null;
  let timer = null;
  let generation = 0;
  let tree = null;
  let currentPromise = Promise.resolve();
  const roomState = new Map();

  container.innerHTML = `${styles}<section aria-label="${esc(title)}"><div class="sc-head"><div><h2>${esc(title)}</h2><p class="sc-hierarchy">Workspace/room → job tabs → agents</p></div><label class="sc-room">Collaboration room<select data-supervision-room disabled><option>Loading rooms…</option></select></label></div><p class="sc-status" data-supervision-status data-state="loading" role="status">Loading collaboration rooms…</p><div class="sc-tree" data-supervision-tree></div></section>`;
  const selector = container.querySelector('[data-supervision-room]');
  const status = container.querySelector('[data-supervision-status]');
  const treeHost = container.querySelector('[data-supervision-tree]');

  const setStatus = (state, message) => {
    status.dataset.state = state;
    status.textContent = message;
  };
  const saveTree = () => {
    if (tree && selectedRoom) roomState.set(selectedRoom, tree.state());
  };
  const clearTree = ({save = true} = {}) => {
    if (save) saveTree();
    tree?.dispose(); tree = null;
    treeHost.innerHTML = '';
  };
  const stopRequest = () => { request?.abort(); request = null; };
  const stopTimer = () => { if (timer !== null) cancelSchedule(timer); timer = null; };
  const schedulePoll = () => {
    stopTimer();
    if (disposed || !selectedRoom || documentImpl?.hidden) return;
    timer = schedule(() => {
      timer = null;
      if (!disposed && !documentImpl?.hidden) currentPromise = refresh();
    }, Math.max(5000, Math.min(10000, pollMs)));
  };
  const paintRooms = () => {
    selector.innerHTML = rooms.map(room => `<option value="${esc(room.room_id)}">${esc(room.name)}</option>`).join('');
    selector.disabled = rooms.length < 2;
    selector.value = selectedRoom;
  };

  async function refresh() {
    if (disposed || !selectedRoom || documentImpl?.hidden) return;
    stopTimer(); stopRequest();
    const version = ++generation;
    const controller = new AbortController(); request = controller;
    setStatus('loading', tree ? 'Updating supervision tree…' : 'Loading supervision tree…');
    try {
      const data = await client.request(`/api/v1/collab/rooms/${encodeURIComponent(selectedRoom)}/supervision`, {signal: controller.signal});
      if (disposed || version !== generation) return;
      if (!data || typeof data !== 'object' || text(data.room_id) !== selectedRoom) throw new Error('room projection mismatch');
      if (tree) tree.update(data);
      else tree = treeMount(treeHost, data, roomState.get(selectedRoom) || {});
      setStatus('ready', records(data.jobs).length || records(data.unassigned).length
        ? 'Supervision tree is current.' : 'No supervised jobs in this room yet.');
    } catch (error) {
      if (disposed || version !== generation || error?.name === 'AbortError') return;
      roomState.delete(selectedRoom);
      clearTree({save: false});
      setStatus('error', 'Could not load this room’s supervision tree.');
    } finally {
      if (request === controller) request = null;
      if (!disposed && version === generation) schedulePoll();
    }
  }

  async function loadRooms() {
    roomRequest?.abort();
    const controller = new AbortController(); roomRequest = controller;
    setStatus('loading', 'Loading collaboration rooms…');
    try {
      const payload = await client.request('/api/v1/collab/rooms', {signal: controller.signal});
      if (disposed || roomRequest !== controller) return;
      rooms = selectCatalogRooms(payload);
      if (!rooms.some(room => room.room_id === selectedRoom)) selectedRoom = rooms[0]?.room_id || '';
      paintRooms();
      if (!selectedRoom) {
        roomState.clear(); clearTree({save: false});
        setStatus('empty', 'No collaboration rooms are available for supervision.');
        return;
      }
      await refresh();
    } catch (error) {
      if (disposed || error?.name === 'AbortError') return;
      rooms = []; selectedRoom = ''; roomState.clear(); paintRooms(); clearTree({save: false});
      setStatus('error', 'Could not load collaboration rooms.');
    } finally {
      if (roomRequest === controller) roomRequest = null;
    }
  }

  const changeRoom = event => {
    if (event.target !== selector) return;
    const next = text(selector.value);
    if (!rooms.some(room => room.room_id === next) || next === selectedRoom) return;
    saveTree(); clearTree({save: false}); stopRequest(); stopTimer();
    selectedRoom = next;
    currentPromise = refresh();
  };
  const visibility = () => {
    if (documentImpl.hidden) { stopTimer(); stopRequest(); return; }
    currentPromise = refresh();
  };
  container.addEventListener('change', changeRoom);
  documentImpl?.addEventListener?.('visibilitychange', visibility);
  const ready = loadRooms();
  currentPromise = ready;

  return {
    ready,
    refresh() { currentPromise = refresh(); return currentPromise; },
    settled() { return currentPromise; },
    dispose() {
      disposed = true; generation += 1; stopTimer(); stopRequest(); roomRequest?.abort(); roomRequest = null;
      clearTree();
      container.removeEventListener('change', changeRoom);
      documentImpl?.removeEventListener?.('visibilitychange', visibility);
    },
  };
}
