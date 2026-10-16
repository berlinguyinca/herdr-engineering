// Agent inspector view (spec 0200): DOM glue around the pure renderers in
// herdr-web-components/inspector.js. Mounted inside the existing session detail
// view — it does not replace it. Bounded buffer, throttled rendering, resync on
// stream gaps, and a per-session SSE stream (not the global feed).
import { connectEvents } from './herdr-web-client/events.js';
import { escapeHtml as esc } from './herdr-web-components/util.js';
import {
  CLIENT_BUFFER, INSPECTOR_VIEWS, PAGE_SIZE, mergeEntry, pickLayout,
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
    data: null, events: [], rev: 0, error: null, loaded: false,
  };
  let disposed = false;
  let renderTimer = null;
  let summaryTimer = null;
  let searchTimer = null;
  let stopStream = () => {};
  let ro = null;

  host.innerHTML = '<h3 style="margin-top:20px">Agent inspector</h3><div class="hd-ins" id="ins-root"></div>';
  const rootEl = host.querySelector('#ins-root');

  const prefs = () => (st.data && st.data.prefs) || {};

  async function load({ reset = true } = {}) {
    try {
      const data = await client.getInspector(sessionId, {
        view: st.view, q: st.q, agentId: st.agent, limit: Math.max(st.limit, PAGE_SIZE),
        since_rev: reset ? 0 : st.rev,
      });
      if (disposed) return;
      if (reset) st.events = [];
      for (const e of data.events) mergeEntry(st.events, e);
      st.rev = Math.max(st.rev, data.rev || 0);
      st.data = { ...data, events: st.events };
      if (!st.loaded) {
        st.loaded = true;
        if (data.prefs && !st.userPickedView && data.prefs.default_view !== st.view) {
          st.view = data.prefs.default_view;
          return load();
        }
      }
      st.error = null;
    } catch (err) {
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
    const width = rootEl.clientWidth || 1000;
    const layout = pickLayout(p.layout || 'auto', width);
    const d = st.data;
    const tabs = INSPECTOR_VIEWS.map((v) =>
      `<button class="hd-ins__tab${v.id === st.view ? ' is-active' : ''}" data-view="${v.id}" role="tab" aria-selected="${v.id === st.view}">${esc(v.label)}</button>`).join('');
    const agents = d && d.summary ? d.summary.agents : [];
    const agentSel = agents.length > 1
      ? `<select id="ins-agent" aria-label="Filter by agent"><option value="">All agents</option>${agents.map((a) =>
        `<option value="${esc(a.agent_id)}"${a.agent_id === st.agent ? ' selected' : ''}>${esc(a.agent_id)}${a.parent_agent_id ? ' (sub)' : ''}</option>`).join('')}</select>` : '';
    const body = st.error
      ? `<herdr-error message="${esc(st.error)}"></herdr-error>`
      : !d ? '<herdr-spinner></herdr-spinner>'
        : renderView(st.view, d, p, st.limit);
    const assoc = d ? renderAssociations(d) : '';
    const keepScroll = rootEl.querySelector('.hd-ins__list');
    const prevTop = keepScroll ? keepScroll.scrollTop : 0;
    rootEl.className = `hd-ins hd-ins--${layout} hd-ins--${p.density || 'comfortable'}`;
    rootEl.innerHTML = `
      ${d ? renderSummaryBar(d) : ''}
      <div class="hd-ins__tools" role="tablist">${tabs}
        <input id="ins-q" type="search" placeholder="Search history…" value="${esc(st.q)}" aria-label="Search execution history">
        ${agentSel}<button class="hd-btn" id="ins-prefs">Preferences</button></div>
      ${st.prefsOpen && d ? renderPrefsForm(p, 'this session') : ''}
      ${layout === 'split'
    ? `<div class="hd-ins__cols"><div class="hd-ins__list" tabindex="0">${body}</div><aside class="hd-ins__aside">${assoc}</aside></div>`
    : `${assoc}<div class="hd-ins__list" tabindex="0">${body}</div>`}`;
    const list = rootEl.querySelector('.hd-ins__list');
    if (list) {
      if (p.follow_tail === false) list.scrollTop = prevTop;
    }
    const q = rootEl.querySelector('#ins-q');
    if (document.activeElement && document.activeElement.id === 'ins-q') q.focus();
  }

  rootEl.addEventListener('click', async (ev) => {
    const t = ev.target;
    if (!(t instanceof Element)) return;
    if (t.matches('.hd-ins__tab')) {
      st.view = t.dataset.view; st.userPickedView = true; st.limit = PAGE_SIZE;
      await load();
    } else if (t.id === 'ins-more') {
      st.limit += PAGE_SIZE; await load();
    } else if (t.id === 'ins-prefs') {
      st.prefsOpen = !st.prefsOpen; render();
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
  rootEl.addEventListener('input', (ev) => {
    if (ev.target.id === 'ins-q') {
      clearTimeout(searchTimer);
      const v = ev.target.value;
      searchTimer = setTimeout(() => { st.q = v; st.limit = PAGE_SIZE; load(); }, 250);
    }
  });
  rootEl.addEventListener('change', (ev) => {
    if (ev.target.id === 'ins-agent') { st.agent = ev.target.value; load(); }
  });
  rootEl.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const form = ev.target;
    const out = {};
    for (const el of form.querySelectorAll('[data-pref]')) {
      out[el.dataset.pref] = el.type === 'checkbox' ? el.checked
        : el.type === 'number' ? Number(el.value) : el.value;
    }
    await savePrefs(out);
  });

  async function savePrefs(p) {
    try {
      await client.setInspectorPrefs(sessionId, p);
      st.prefsOpen = false;
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
      if (f.type === 'gap') { load(); return; }
      if (f.type !== 'event' || !f.event) return;
      const kinds = VIEW_KINDS[st.view];
      if (!st.q && !st.agent && (!kinds || kinds.includes(f.event.kind)) && st.data) {
        mergeEntry(st.events, f.event, CLIENT_BUFFER);
        st.rev = Math.max(st.rev, f.event.rev || 0);
        scheduleRender();
      }
      refreshSoon();
    },
    onError: () => {},
  });

  if (typeof ResizeObserver !== 'undefined') {
    ro = new ResizeObserver(() => scheduleRender());
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
