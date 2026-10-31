// Agent inspector view (spec 0200): DOM glue around the pure renderers in
// herdr-web-components/inspector.js. Mounted inside the existing session detail
// view — it does not replace it. Bounded buffer, throttled rendering, resync on
// stream gaps / reconnects / server restarts, and a per-session SSE stream.
//
// The DOM skeleton is built once; live updates only touch the bar, the list and
// the aside, so typing in the search box, an open preferences form, selections
// and scroll position survive streaming.
import { connectEvents } from './herdr-web-client/events.js';
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { skeleton } from './motion.js';
import {
  CLIENT_BUFFER, CLIENT_BUFFER_MAX, INSPECTOR_VIEWS, PAGE_SIZE, mergeEntry, pickLayout,
  renderAssociations, renderPrefsForm, renderSummaryBar, renderView,
} from './herdr-web-components/inspector.js';

const VIEW_KINDS = {
  conversation: ['message'], reasoning: ['reasoning'], tools: ['tool'],
  files: ['file'], tests: ['test'], usage: ['usage', 'model'],
};
const RENDER_MS = 150;
const SUMMARY_MS = 2000;

export function mountInspector(host, client, sessionId) {
  const st = {
    view: 'conversation', q: '', agent: '', limit: PAGE_SIZE, prefsOpen: false,
    data: null, events: [], rev: 0, epoch: null, error: null, loaded: false,
    layout: null, agentsSig: '', userPickedView: false,
  };
  let disposed = false;
  let reqId = 0;               // latest issued load(); older responses are discarded
  let renderTimer = null;
  let summaryTimer = null;
  let searchTimer = null;
  let stopStream = () => {};
  let ro = null;

  host.innerHTML = `<h3 style="margin-top:20px">Agent inspector</h3>
    <div class="hd-ins" id="ins-root">
      <div id="ins-bar"></div>
      <div class="hd-ins__tools" role="tablist" id="ins-tabs">
        ${INSPECTOR_VIEWS.map((v) => `<button class="hd-ins__tab" data-view="${v.id}" role="tab">${esc(v.label)}</button>`).join('')}
        <input id="ins-q" type="search" placeholder="Search history…" aria-label="Search execution history">
        <select id="ins-agent" aria-label="Filter by agent" hidden></select>
        <button class="hd-btn" id="ins-prefs">Preferences</button>
      </div>
      <div id="ins-prefs-box"></div>
      <div class="hd-ins__cols">
        <div class="hd-ins__list" id="ins-list" tabindex="0"></div>
        <aside class="hd-ins__aside" id="ins-aside"></aside>
      </div>
    </div>`;
  const $ = (id) => host.querySelector(`#${id}`);
  const rootEl = $('ins-root');
  const listEl = $('ins-list');

  const prefs = () => (st.data && st.data.prefs) || {};
  const cap = () => Math.min(CLIENT_BUFFER_MAX, Math.max(CLIENT_BUFFER, st.limit));

  async function load({ reset = true } = {}) {
    const my = ++reqId;
    const sent = { view: st.view, q: st.q, agent: st.agent };
    try {
      const data = await client.getInspector(sessionId, {
        view: st.view, q: st.q, agentId: st.agent, limit: Math.max(st.limit, PAGE_SIZE),
        since_rev: reset ? 0 : st.rev,
      });
      // Stale: a newer load started, or the filters changed while this was in flight.
      if (disposed || my !== reqId || sent.view !== st.view || sent.q !== st.q || sent.agent !== st.agent) return;
      const restarted = st.epoch && data.epoch && st.epoch !== data.epoch;
      if (!reset && (data.has_more || restarted)) return load();   // would skip changes: resync fully
      if (reset) st.events = [];
      for (const e of data.events) mergeEntry(st.events, e, cap());
      st.rev = reset ? (data.rev || 0) : Math.max(st.rev, data.rev || 0);
      st.epoch = data.epoch || st.epoch;
      st.data = { ...data, events: st.events };
      st.error = null;
      if (!st.loaded) {
        st.loaded = true;
        if (data.prefs && !st.userPickedView && data.prefs.default_view !== st.view) {
          st.view = data.prefs.default_view;
          return load();
        }
      }
    } catch (err) {
      if (my !== reqId) return;
      st.error = err.message || String(err);
    }
    scheduleRender();
  }

  function scheduleRender() {
    if (renderTimer || disposed) return;
    renderTimer = setTimeout(() => { renderTimer = null; render(); }, RENDER_MS);
  }

  function render() {
    if (disposed) return;
    const p = prefs();
    const d = st.data;
    st.layout = pickLayout(p.layout || 'auto', rootEl.clientWidth || 1000, st.layout);
    rootEl.className = `hd-ins hd-ins--${st.layout} hd-ins--${p.density || 'comfortable'}`;

    $('ins-bar').innerHTML = d ? renderSummaryBar(d) : '';
    for (const b of host.querySelectorAll('.hd-ins__tab')) {
      const on = b.dataset.view === st.view;
      b.classList.toggle('is-active', on);
      b.setAttribute('aria-selected', String(on));
    }
    // Agent filter: rebuild only when the set of agents changed and it is not in use.
    const agents = d && d.summary ? d.summary.agents : [];
    const sel = $('ins-agent');
    const sig = agents.map((a) => a.agent_id).join('|');
    sel.hidden = agents.length < 2;
    if (sig !== st.agentsSig && document.activeElement !== sel) {
      st.agentsSig = sig;
      sel.innerHTML = '<option value="">All agents</option>' + agents.map((a) =>
        `<option value="${esc(a.agent_id)}">${esc(a.agent_id)}${a.parent_agent_id ? ' (sub)' : ''}</option>`).join('');
      sel.value = st.agent;
    }

    // List: keep the reader's place. Newest rows are inserted at the top, so
    // compensate for the growth unless the reader is already at the top.
    const prevTop = listEl.scrollTop;
    const prevHeight = listEl.scrollHeight;
    listEl.innerHTML = st.error
      ? `<herdr-error message="${esc(st.error)}"></herdr-error>`
      : !d ? skeleton('inspection', { rows: 3 }) : renderView(st.view, d, p, st.limit);
    if (prevTop > 4) listEl.scrollTop = prevTop + (listEl.scrollHeight - prevHeight);
    $('ins-aside').innerHTML = d ? renderAssociations(d) : '';
  }

  // ---- events (delegated; the skeleton never changes so listeners stay valid)
  rootEl.addEventListener('click', async (ev) => {
    const t = ev.target;
    if (!(t instanceof Element)) return;
    if (t.matches('.hd-ins__tab')) {
      st.view = t.dataset.view; st.userPickedView = true; st.limit = PAGE_SIZE;
      render(); await load();
    } else if (t.id === 'ins-more') {
      st.limit += PAGE_SIZE; await load();
    } else if (t.id === 'ins-prefs') {
      st.prefsOpen = !st.prefsOpen;
      $('ins-prefs-box').innerHTML = st.prefsOpen && st.data ? renderPrefsForm(prefs(), 'this session') : '';
    } else if (t.id === 'ins-show-reasoning') {
      await savePrefs({ show_reasoning: true });
    } else if (t.classList.contains('hd-ins__expand')) {
      t.disabled = true;
      try {
        const full = await client.getInspectorEvent(sessionId, Number(t.dataset.seq));
        const i = st.events.findIndex((e) => e.seq === full.seq);
        if (i >= 0) st.events[i] = { ...full, data: { ...full.data, output_more: false, diff_more: false } };
        scheduleRender();
      } catch (err) { t.textContent = `Failed: ${err.message}`; }
    }
  });
  $('ins-q').addEventListener('input', (ev) => {
    st.q = ev.target.value;                       // immediate: renders never overwrite the box
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { st.limit = PAGE_SIZE; load(); }, 250);
  });
  $('ins-agent').addEventListener('change', (ev) => { st.agent = ev.target.value; load(); });
  rootEl.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const out = {};
    for (const el of ev.target.querySelectorAll('[data-pref]')) {
      out[el.dataset.pref] = el.type === 'checkbox' ? el.checked
        : el.type === 'number' ? Number(el.value) : el.value;
    }
    await savePrefs(out);
  });

  async function savePrefs(p) {
    try {
      await client.setInspectorPrefs(sessionId, p);
      st.prefsOpen = false;
      $('ins-prefs-box').innerHTML = '';
      await load();
    } catch (err) {
      st.error = err.status === 403 ? 'Changing preferences requires the operator role.' : err.message;
      scheduleRender();
    }
  }

  function refreshSoon() {
    if (summaryTimer) return;
    summaryTimer = setTimeout(async () => { summaryTimer = null; await load({ reset: false }); }, SUMMARY_MS);
  }

  stopStream = connectEvents({
    url: client.inspectorStreamUrl(sessionId),
    onEvent: (f) => {
      if (disposed) return;
      if (f.type === 'connected') {
        // The first snapshot may predate the subscription. Resync on every
        // connection; reqId prevents that older response replacing newer data.
        load();
        return;
      }
      if (f.type === 'gap') { load(); return; }
      if (f.type !== 'event' || !f.event) return;
      const kinds = VIEW_KINDS[st.view];
      if (!st.q && !st.agent && (!kinds || kinds.includes(f.event.kind)) && st.data) {
        mergeEntry(st.events, f.event, cap());
        // Only HTTP snapshots advance the synchronization cursor. A later
        // streamed row does not prove earlier rows were received.
        scheduleRender();
      }
      refreshSoon();
    },
    onError: () => {},
  });

  if (typeof ResizeObserver !== 'undefined') {
    ro = new ResizeObserver(() => {
      const next = pickLayout(prefs().layout || 'auto', rootEl.clientWidth || 1000, st.layout);
      if (next !== st.layout) scheduleRender();    // only a real breakpoint crossing re-renders
    });
    ro.observe(rootEl);
  }
  load();

  return function dispose() {
    disposed = true;
    clearTimeout(renderTimer); clearTimeout(summaryTimer); clearTimeout(searchTimer);
    stopStream();
    if (ro) ro.disconnect();
  };
}
